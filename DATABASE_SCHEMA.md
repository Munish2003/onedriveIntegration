# PostgreSQL Database Schema Design (Finalized Names)

This schema uses `companies` instead of `organizations` and `employee` instead of `users`, as requested. It maintains the full integration requirements for OneDrive and vector embeddings.

## Entity Relationship Diagram (Conceptual)
1. **Companies** (1) -> (M) **Employees**
2. **Companies** (1) -> (M) **OneDrive Connections**
3. **OneDrive Connection** (1) -> (M) **Tracked Folders**
4. **Companies** (1) -> (M) **Documents**
5. **Document** (1) -> (M) **Document Embeddings**

---

## SQL Code (Table creation scripts)

```sql
-- Enable the vector extension for embeddings if needed
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. COMPANIES
CREATE TABLE companies (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),          -- Plaintext (Relational ID)
    name TEXT NOT NULL,                                      -- ENCRYPTED (AES-256-GCM + Base64)
    tenant_id TEXT,                                          -- ENCRYPTED (AES-256-GCM + Base64)
    client_id TEXT,                                          -- ENCRYPTED (AES-256-GCM + Base64)
    primary_email TEXT,                                      -- ENCRYPTED (AES-256-GCM + Base64)
    domain TEXT,                                             -- ENCRYPTED (AES-256-GCM + Base64)
    total_quota BIGINT DEFAULT 0,                            -- Plaintext (Number)
    consumed_quota BIGINT DEFAULT 0,                         -- Plaintext (Number)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. EMPLOYEE
CREATE TABLE employee (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),          -- Plaintext (Relational ID)
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email TEXT UNIQUE NOT NULL,                              -- ENCRYPTED (AES-256-GCM + Base64)
    name TEXT,                                               -- ENCRYPTED (AES-256-GCM + Base64)
    role TEXT DEFAULT 'member',                              -- Plaintext (Standard Roles)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. ONEDRIVE CONNECTIONS
CREATE TABLE onedrive_connections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    employee_id UUID NOT NULL REFERENCES employee(id) ON DELETE SET NULL,
    drive_id TEXT NOT NULL,                                  -- ENCRYPTED (AES-256-GCM + Base64)
    access_type TEXT NOT NULL DEFAULT 'specific_folders',    -- ENCRYPTED (AES-256-GCM + Base64)
    access_token TEXT,                                       -- ENCRYPTED (AES-256-GCM + Base64)
    refresh_token TEXT,                                      -- ENCRYPTED (AES-256-GCM + Base64)
    token_expires_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(company_id, drive_id)
);

-- 4. TRACKED FOLDERS
CREATE TABLE tracked_folders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    onedrive_connection_id UUID NOT NULL REFERENCES onedrive_connections(id) ON DELETE CASCADE,
    folder_id TEXT NOT NULL,                                 -- ENCRYPTED (AES-256-GCM + Base64)
    folder_name TEXT NOT NULL,                               -- ENCRYPTED (AES-256-GCM + Base64)
    delta_link TEXT,                                         -- ENCRYPTED (AES-256-GCM + Base64)
    subscription_id TEXT,                                    -- ENCRYPTED (AES-256-GCM + Base64)
    subscription_expires_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(onedrive_connection_id, folder_id)
);

-- 5. DOCUMENTS
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    onedrive_connection_id UUID NOT NULL REFERENCES onedrive_connections(id) ON DELETE CASCADE,
    tracked_folder_id UUID NOT NULL REFERENCES tracked_folders(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,                                   -- ENCRYPTED (AES-256-GCM + Base64)
    file_name TEXT NOT NULL,                                 -- ENCRYPTED (AES-256-GCM + Base64)
    mime_type TEXT,                                          -- ENCRYPTED (AES-256-GCM + Base64)
    size_bytes BIGINT,                                       -- Plaintext (Size)
    last_modified_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tracked_folder_id, file_id)
);

-- 6. DOCUMENT EMBEDDINGS
CREATE TABLE document_embeddings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    onedrive_connection_id UUID NOT NULL REFERENCES onedrive_connections(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL, 
    text_content TEXT NOT NULL,                              -- ENCRYPTED (AES-256-GCM + Base64)
    embedding VECTOR(1536),                                  -- Plaintext (Required for Similarity search)
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- INDEXES
CREATE INDEX idx_employee_company ON employee(company_id);
CREATE INDEX idx_onedrive_company ON onedrive_connections(company_id);
CREATE INDEX idx_folders_conn ON tracked_folders(onedrive_connection_id);
CREATE INDEX idx_docs_folder ON documents(tracked_folder_id);
CREATE INDEX idx_embeddings_doc ON document_embeddings(document_id);
CREATE INDEX idx_embeddings_company ON document_embeddings(company_id);
CREATE INDEX idx_embeddings_vector ON document_embeddings USING hnsw (embedding vector_cosine_ops);
```

## Best Standard Encryption Strategy: AES-256-GCM

To ensure "even the developers cannot see the data," we use **Application-Side Encryption**. This means the data is encrypted in Python **before** it touches the database, and decrypted in Python **after** it is read.

### 1. The Standard: AES-256-GCM
*   **AES (Advanced Encryption Standard)**: The gold standard for symmetric encryption.
*   **256-bit Key**: Extremely secure (government-grade).
*   **GCM (Galois/Counter Mode)**: Provides both **confidentiality** and **authenticity** (it ensures the data hasn't been tampered with).

### 2. Implementation Logic
*   **Master Key**: A single 32-byte key stored securely in your `.env`.
*   **Initialization Vector (IV)**: A unique, random 12-byte value prepended to every encrypted string. This ensures that the same name (e.g., "John") results in a different encrypted blob every time.
*   **Storage**: In PostgreSQL, all encrypted columns are set to **`TEXT`** type to hold the Base64-encoded encrypted strings.
    - **Visibility**: This allows you to see the encrypted data as random characters in your database tools.
    - **Convenience**: Base64 is the standard way to represent binary encryption in text-based environments.

### 3. What is NOT Encrypted (and why)
| Column Type | Encryption Status | Rationale |
| :--- | :--- | :--- |
| **UUIDs (IDs)** | Plaintext | PostgreSQL needs these to maintain relationships and link tables. Encrypting them breaks standard SQL functionality. |
| **Timestamps** | Plaintext | Required for auditing, sorting, and database performance. |
| **Numbers (Quota)** | Plaintext | Required for mathematical operations (SQL `SUM`, `COUNT`) within the database. |
| **Vectors** | **Plaintext** | **CRITICAL**: Similarity search (semantic search) requires comparing raw numbers. If vectors are encrypted, your AI search will fail. |

### 4. Impact on Reading/Writing
*   **Performance**: AES-256-GCM is hardware-accelerated on most modern CPUs. The impact is negligible (a few milliseconds).
*   **Searchability**: You **cannot** use SQL `WHERE name LIKE '%John%'` on encrypted columns. Searching must be done by fetching the data or using deterministic encryption (specialized setup).
