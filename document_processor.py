"""
Document Processor — OneDrive File Processing Pipeline
Downloads files from OneDrive, extracts text in-memory, generates embeddings,
encrypts sensitive data, and stores results in PostgreSQL.
Also handles sync: detects files deleted from OneDrive and removes them from PG.
"""

import os
import io
import uuid
import asyncio
import logging
from typing import List, Optional, Set
from datetime import datetime

import aiohttp
from pypdf import PdfReader
import docx2txt
from pptx import Presentation
from langchain_openai import AzureOpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from dotenv import load_dotenv

from database import db, encryptor

load_dotenv()
logger = logging.getLogger("DocProcessor")

# ==============================================================================
# CONFIGURATION
# ==============================================================================
class ProcessorConfig:
    AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
    AZURE_OPENAI_API_INSTANCE_NAME = os.getenv("AZURE_OPENAI_API_INSTANCE_NAME")
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")
    AZURE_OPENAI_EMBEDDING_MODEL = os.getenv("AZURE_OPENAI_EMBEDDING_MODEL")
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1000"))
    CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))
    EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "50"))
    MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))

proc_config = ProcessorConfig()

# Supported file types
SUPPORTED_TYPES = {"pdf", "docx", "pptx"}

# Embeddings model (lazily initialized)
_embeddings_model = None

def get_embeddings_model() -> AzureOpenAIEmbeddings:
    global _embeddings_model
    if _embeddings_model is None:
        _embeddings_model = AzureOpenAIEmbeddings(
            api_key=proc_config.AZURE_OPENAI_API_KEY,
            azure_endpoint=proc_config.AZURE_OPENAI_API_INSTANCE_NAME,
            azure_deployment=proc_config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
            api_version=proc_config.AZURE_OPENAI_API_VERSION,
            model=proc_config.AZURE_OPENAI_EMBEDDING_MODEL,
        )
    return _embeddings_model


# ==============================================================================
# TEXT EXTRACTION (In-memory, no temp files)
# ==============================================================================
def get_file_type(file_name: str) -> Optional[str]:
    """Get file type from extension, return None if unsupported."""
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return ext if ext in SUPPORTED_TYPES else None


def extract_text_from_bytes(file_bytes: bytes, doc_type: str) -> str:
    """Extract text from raw bytes using BytesIO — 100% in-memory."""
    max_bytes = proc_config.MAX_FILE_SIZE_MB * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise ValueError(f"File exceeds {proc_config.MAX_FILE_SIZE_MB}MB limit")

    stream = io.BytesIO(file_bytes)

    if doc_type == "pdf":
        reader = PdfReader(stream)
        text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        logger.info(f"  Extracted {len(reader.pages)} pages from PDF")

    elif doc_type == "docx":
        text = docx2txt.process(stream).strip()
        logger.info(f"  Extracted text from DOCX")

    elif doc_type == "pptx":
        prs = Presentation(stream)
        parts = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    parts.append(shape.text_frame.text)
        text = "\n".join(parts).strip()
        logger.info(f"  Extracted {len(prs.slides)} slides from PPTX")
    else:
        raise ValueError(f"Unsupported type: {doc_type}")

    stream.close()

    if not text:
        raise ValueError(f"No text content extracted from {doc_type.upper()}")

    return text


# ==============================================================================
# ONEDRIVE FILE DOWNLOAD
# ==============================================================================
async def download_file_from_onedrive(
    access_token: str, drive_id: str, file_id: str
) -> bytes:
    """Download a single file from OneDrive as raw bytes via Graph API."""
    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{file_id}/content"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise ValueError(f"Download failed ({resp.status}): {err}")
            return await resp.read()


async def list_folder_files_recursive(
    access_token: str, drive_id: str, item_id: str
) -> List[dict]:
    """List all files recursively in a folder using Delta API."""
    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/delta"
    headers = {"Authorization": f"Bearer {access_token}"}
    all_files = []

    async with aiohttp.ClientSession() as session:
        while url:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"Delta API error: {await resp.text()}")
                    break
                data = await resp.json()
                for item in data.get("value", []):
                    if item.get("deleted") or "@removed" in item:
                        continue
                    if item.get("id") == item_id:
                        continue
                    if "file" in item:
                        all_files.append(item)
                url = data.get("@odata.nextLink")

    return all_files


# ==============================================================================
# EMBEDDING GENERATION (Batched)
# ==============================================================================
async def embed_texts_batch(texts: List[str]) -> List[List[float]]:
    """Generate embeddings in batches using Azure OpenAI."""
    model = get_embeddings_model()
    batch_size = proc_config.EMBEDDING_BATCH_SIZE
    result = []
    total = len(texts)
    logger.info(f"  Generating embeddings for {total} chunks...")

    for i in range(0, total, batch_size):
        batch_num = (i // batch_size) + 1
        batch = texts[i:i + batch_size]
        try:
            embeddings = await asyncio.to_thread(model.embed_documents, batch)
            result.extend(embeddings)
            logger.info(f"    ✓ Batch {batch_num}/{(total + batch_size - 1) // batch_size}")
        except Exception as e:
            logger.error(f"    ✗ Batch {batch_num} failed: {e}")
            raise
    return result


# ==============================================================================
# SINGLE FILE PROCESSING
# ==============================================================================
async def process_single_file(
    access_token: str,
    drive_id: str,
    company_id: str,
    onedrive_connection_id: str,
    tracked_folder_id: str,
    file_item: dict,
) -> Optional[str]:
    """
    Process a single file: download → extract → chunk → embed → encrypt → store.
    Returns the document UUID if successful, None if skipped.
    """
    file_id = file_item["id"]
    file_name = file_item.get("name", "unknown")
    mime_type = file_item.get("file", {}).get("mimeType", "")
    size_bytes = file_item.get("size", 0)
    last_modified = file_item.get("lastModifiedDateTime")

    file_type = get_file_type(file_name)
    if not file_type:
        logger.info(f"  ⏭️  Skipping unsupported file: {file_name}")
        return None

    try:
        # 1. Download raw bytes from OneDrive
        logger.info(f"  📥 Downloading: {file_name} ({size_bytes} bytes)")
        file_bytes = await download_file_from_onedrive(access_token, drive_id, file_id)

        # 2. Extract text in-memory
        text = await asyncio.to_thread(extract_text_from_bytes, file_bytes, file_type)

        # 3. Chunk text
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=proc_config.CHUNK_SIZE,
            chunk_overlap=proc_config.CHUNK_OVERLAP,
            add_start_index=True,
        )
        chunks = splitter.split_text(text)
        if not chunks:
            logger.warning(f"  ⚠️  No chunks from: {file_name}")
            return None

        logger.info(f"  ✂️  Created {len(chunks)} chunks from: {file_name}")

        # 4. Generate embeddings
        embeddings = await embed_texts_batch(chunks)

        # 5. Encrypt sensitive fields
        enc_file_id = encryptor.encrypt(file_id) if encryptor else file_id
        enc_file_name = encryptor.encrypt(file_name) if encryptor else file_name
        enc_mime_type = encryptor.encrypt(mime_type) if encryptor else mime_type

        # 6. Parse last_modified
        last_mod_dt = None
        if last_modified:
            try:
                last_mod_dt = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
            except Exception:
                pass

        # 7. Upsert into PostgreSQL (delete old + insert new in a transaction)
        doc_uuid = str(uuid.uuid4())
        pool = await db.get_pool()

        async with pool.acquire() as conn:
            async with conn.transaction():
                # Delete any existing document with same file_id for this folder
                await conn.execute(
                    "DELETE FROM documents WHERE tracked_folder_id = $1 AND file_id = $2",
                    uuid.UUID(tracked_folder_id), enc_file_id
                )

                # Insert document record
                await conn.execute(
                    """INSERT INTO documents 
                       (id, company_id, onedrive_connection_id, tracked_folder_id,
                        file_id, file_name, mime_type, size_bytes, last_modified_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                    uuid.UUID(doc_uuid),
                    uuid.UUID(company_id),
                    uuid.UUID(onedrive_connection_id),
                    uuid.UUID(tracked_folder_id),
                    enc_file_id, enc_file_name, enc_mime_type,
                    size_bytes, last_mod_dt
                )

                # Insert embeddings
                embedding_rows = []
                for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                    enc_chunk = encryptor.encrypt(chunk) if encryptor else chunk
                    emb_str = "[" + ",".join(map(str, emb)) + "]"
                    embedding_rows.append((
                        uuid.uuid4(),
                        uuid.UUID(company_id),
                        uuid.UUID(onedrive_connection_id),
                        uuid.UUID(doc_uuid),
                        idx,
                        enc_chunk,
                        emb_str,
                    ))

                await conn.executemany(
                    """INSERT INTO document_embeddings
                       (id, company_id, onedrive_connection_id, document_id,
                        chunk_index, text_content, embedding)
                       VALUES ($1, $2, $3, $4, $5, $6, $7::vector)""",
                    embedding_rows
                )

        logger.info(f"  ✅ Stored: {file_name} ({len(chunks)} chunks, doc_id: {doc_uuid})")
        return doc_uuid

    except Exception as e:
        logger.error(f"  ❌ Failed to process {file_name}: {e}")
        return None


# ==============================================================================
# BATCH FOLDER PROCESSING (with sync detection)
# ==============================================================================
async def process_folder_batch(
    access_token: str,
    drive_id: str,
    company_id: str,
    onedrive_connection_id: str,
    tracked_folder_id: str,
    folder_item_id: str,
):
    """
    Batch process all files in a connected folder.
    1. Lists all files recursively from OneDrive
    2. Processes each supported file (download → extract → embed → encrypt → store)
    3. SYNC: Deletes any files from PG that no longer exist in OneDrive
    """
    logger.info(f"🚀 Starting batch processing for folder {folder_item_id}...")

    # Step 1: List all files currently in OneDrive
    onedrive_files = await list_folder_files_recursive(access_token, drive_id, folder_item_id)
    logger.info(f"📂 Found {len(onedrive_files)} files in OneDrive folder")

    # Filter to supported types only
    supported_files = [f for f in onedrive_files if get_file_type(f.get("name", ""))]
    logger.info(f"📄 {len(supported_files)} supported files (PDF/DOCX/PPTX)")

    # Step 2: Process each file
    success_count = 0
    skip_count = 0
    fail_count = 0
    processed_file_ids: Set[str] = set()

    for file_item in supported_files:
        file_id = file_item["id"]
        # Keep track of the ENCRYPTED file_id we store in PG
        enc_file_id = encryptor.encrypt(file_id) if encryptor else file_id
        processed_file_ids.add(enc_file_id)

        result = await process_single_file(
            access_token=access_token,
            drive_id=drive_id,
            company_id=company_id,
            onedrive_connection_id=onedrive_connection_id,
            tracked_folder_id=tracked_folder_id,
            file_item=file_item,
        )
        if result:
            success_count += 1
        else:
            skip_count += 1

    # Step 3: SYNC — Delete files from PG that no longer exist in OneDrive
    if processed_file_ids:
        existing_docs = await db.fetch(
            "SELECT id, file_id FROM documents WHERE tracked_folder_id = $1",
            uuid.UUID(tracked_folder_id)
        )
        stale_doc_ids = []
        for doc in existing_docs:
            if doc["file_id"] not in processed_file_ids:
                stale_doc_ids.append(doc["id"])

        if stale_doc_ids:
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for doc_id in stale_doc_ids:
                        await conn.execute(
                            "DELETE FROM documents WHERE id = $1", doc_id
                        )
            logger.info(f"🧹 SYNC: Removed {len(stale_doc_ids)} stale documents from PG")

    logger.info(
        f"✅ Batch complete: {success_count} processed, {skip_count} skipped, {fail_count} failed"
    )
    return {
        "processed": success_count,
        "skipped": skip_count,
        "failed": fail_count,
        "synced_deleted": len(stale_doc_ids) if processed_file_ids else 0,
    }


# ==============================================================================
# DELETE OPERATIONS
# ==============================================================================
async def delete_folder_data(tracked_folder_id: str):
    """
    Delete ALL data for a tracked folder from PG.
    Due to ON DELETE CASCADE, deleting from tracked_folders will also delete:
      → documents → document_embeddings
    """
    logger.info(f"🗑️  Deleting all data for folder: {tracked_folder_id}")
    await db.execute(
        "DELETE FROM tracked_folders WHERE id = $1",
        uuid.UUID(tracked_folder_id)
    )
    logger.info(f"✅ Folder data deleted (cascaded to documents + embeddings)")


async def delete_file_by_onedrive_id(
    tracked_folder_id: str, onedrive_file_id: str
):
    """
    Delete a single file (and its embeddings) from PG by its OneDrive file_id.
    Used by webhook when a file is deleted from OneDrive.
    """
    enc_file_id = encryptor.encrypt(onedrive_file_id) if encryptor else onedrive_file_id

    result = await db.execute(
        "DELETE FROM documents WHERE tracked_folder_id = $1 AND file_id = $2",
        uuid.UUID(tracked_folder_id), enc_file_id
    )
    logger.info(f"🗑️  Deleted file {onedrive_file_id} from PG (cascade to embeddings)")
    return result
