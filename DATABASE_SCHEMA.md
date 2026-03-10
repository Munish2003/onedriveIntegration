# PostgreSQL Database Schema Design

Based on your requirements, here is a robust, production-ready PostgreSQL table structure. It uses UUIDs for primary keys, foreign keys for strict relationships, and includes standard audit timestamps (`created_at`, `updated_at`). 

It also assumes you might use `pgvector` for storing the embeddings.

## Entity Relationship Diagram (Conceptual)
1. **Organization** (1) -> (M) **Users**
2. **Organization** (1) -> (M) **OneDrive Connections**
3. **OneDrive Connection** (1) -> (M) **Tracked Folders**
4. **Tracked Folder** (1) -> (M) **Documents**
5. **Document** (1) -> (M) **Document Embeddings**

---

## SQL Code (Table creation scripts)

```sql
-- Enable the vector extension for embeddings if needed
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. ORGANIZATIONS
CREATE TABLE organizations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    tenant_id VARCHAR(255),
    client_id VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. USERS
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255),
    role VARCHAR(50) DEFAULT 'member', -- e.g., admin, member
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. ONEDRIVE CONNECTIONS
-- Represents an authenticated link to a OneDrive Drive
CREATE TABLE onedrive_connections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE SET NULL, -- Who authenticated this
    drive_id VARCHAR(255) NOT NULL,
    access_type VARCHAR(50) NOT NULL DEFAULT 'specific_folders' CHECK (access_type IN ('full_access', 'specific_folders')),
    access_token TEXT,  -- Consider encrypting these at rest
    refresh_token TEXT, -- Consider encrypting these at rest
    token_expires_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(organization_id, drive_id)
);

-- 4. TRACKED FOLDERS
-- Specific folders inside a OneDrive connection that the user selected
CREATE TABLE tracked_folders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    onedrive_connection_id UUID NOT NULL REFERENCES onedrive_connections(id) ON DELETE CASCADE,
    folder_id VARCHAR(255) NOT NULL, -- Microsoft Graph Item ID
    folder_name VARCHAR(255) NOT NULL,
    delta_link TEXT, -- To track changes using Microsoft's Delta API
    subscription_id VARCHAR(255), -- Webhook subscription ID
    subscription_expires_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(onedrive_connection_id, folder_id)
);

-- 5. DOCUMENTS
-- The actual files discovered inside the tracked folders
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    onedrive_connection_id UUID NOT NULL REFERENCES onedrive_connections(id) ON DELETE CASCADE,
    tracked_folder_id UUID NOT NULL REFERENCES tracked_folders(id) ON DELETE CASCADE,
    file_id VARCHAR(255) NOT NULL, -- Microsoft Graph Item ID
    file_name VARCHAR(255) NOT NULL,
    mime_type VARCHAR(100),
    size_bytes BIGINT,
    last_modified_at TIMESTAMP WITH TIME ZONE, -- Microsoft Graph LastModifiedDateTime
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tracked_folder_id, file_id)
);

-- 6. DOCUMENT EMBEDDINGS
-- The text chunks extracted from documents, converted to vector embeddings
CREATE TABLE document_embeddings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    onedrive_connection_id UUID NOT NULL REFERENCES onedrive_connections(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL, -- If a document is split into multiple parts
    text_content TEXT NOT NULL,   -- The raw scraped text
    embedding VECTOR(1536),       -- Adjust dimensions based on your AI model (e.g. 1536 for OpenAI)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- INDEXES (Crucial for query performance)
CREATE INDEX idx_users_org ON users(organization_id);
CREATE INDEX idx_onedrive_org ON onedrive_connections(organization_id);
CREATE INDEX idx_folders_conn ON tracked_folders(onedrive_connection_id);
CREATE INDEX idx_docs_folder ON documents(tracked_folder_id);
CREATE INDEX idx_embeddings_doc ON document_embeddings(document_id);
CREATE INDEX idx_embeddings_org ON document_embeddings(organization_id);

-- Optional: Vector similarity search index (pgvector HNSW)
CREATE INDEX idx_embeddings_vector ON document_embeddings USING hnsw (embedding vector_cosine_ops);
```

## Why this structure is perfectly suited for you:
1. **Cascading Deletes:** Notice the `ON DELETE CASCADE`. If a user disconnects an organization, or disconnects a `tracked_folder`, PostgreSQL will **automatically** delete all associated `documents` and `document_embeddings`. You won't have to manually write clean-up scripts!
2. **Delta Links:** The `delta_link` and webhook Data is safely stored per folder inside the `tracked_folders` table, replacing your current hardcoded in-memory session.
3. **Multi-Tenancy:** Every crucial table is linked back to `organization_id`. This means you can trivially filter any query `WHERE organization_id = '...'` to guarantee one company's data never leaks into another company's view.
4. **Vector Search Ready:** Utilizing the `pgvector` extension directly in PostgreSQL so you don't need a separate expensive database like Pinecone just for embeddings.
