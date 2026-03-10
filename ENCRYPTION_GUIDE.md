# Zoxima Database Encryption Guide (AES-256-GCM)

To ensure that "even the developers cannot see the data" in the PostgreSQL database, we implement **Application-Side Encryption**. This guide provides the standard Python implementation using the `cryptography` library.

### 1. Requirements
Add this to your `requirements.txt`:
```text
cryptography==42.0.5
```

### 2. The Encryption Helper Class
This class handles the AES-256-GCM logic. It ensures that every piece of data has a unique **Initialization Vector (IV)**, so even identical names result in different encrypted strings in the database.

```python
import os
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class DatabaseEncryptor:
    def __init__(self, master_key_hex: str):
        # Master key must be 32 bytes (64 hex characters) for AES-256
        self.key = bytes.fromhex(master_key_hex)
        self.aesgcm = AESGCM(self.key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypts a string and returns a Base64 encoded blob (IV + Ciphertext + Tag)."""
        if not plaintext:
            return ""
        
        # 1. Generate a random 12-byte nonce (IV)
        nonce = os.urandom(12)
        
        # 2. Encrypt the data
        ciphertext = self.aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
        
        # 3. Combine Nonce + Ciphertext and encode to Base64 for storage in TEXT column
        return base64.b64encode(nonce + ciphertext).decode('utf-8')

    def decrypt(self, encrypted_blob: str) -> str:
        """Decrypts a Base64 blob and returns the original plaintext."""
        if not encrypted_blob:
            return ""
            
        try:
            # 1. Decode from Base64
            data = base64.b64decode(encrypted_blob.encode('utf-8'))
            
            # 2. Extract the 12-byte nonce and the rest (ciphertext + tag)
            nonce = data[:12]
            ciphertext = data[12:]
            
            # 3. Decrypt
            plaintext = self.aesgcm.decrypt(nonce, ciphertext, None)
            return plaintext.decode('utf-8')
        except Exception as e:
            print(f"Decryption failed: {e}")
            return "[DECRYPTION_FAILED]"

# Usage Example:
# MASTER_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
# encryptor = DatabaseEncryptor(MASTER_KEY)
# encrypted_name = encryptor.encrypt("Zoxima Corp")
# decrypted_name = encryptor.decrypt(encrypted_name)
```

### 3. Transparent Integration (How to not affect logic)
To make this "not affect reading/writing," you should wrap your database access layer.

**A. When Writing (Insert/Update):**
```python
# Before sending to PostgreSQL:
employee_data = {
    "name": encryptor.encrypt(user_input_name),
    "email": encryptor.encrypt(user_input_email),
    "company_id": company_id  # Plaintext ID stays same
}
# db.execute("INSERT INTO employee ...", employee_data)
```

**B. When Reading (Select):**
```python
# After fetching from PostgreSQL:
row = db.fetchone()
employee = {
    "id": row["id"],
    "name": encryptor.decrypt(row["name"]),
    "email": encryptor.decrypt(row["email"])
}
```

### 4. Key Security
> [!IMPORTANT]
> **Who has the key has the data.**
> - Store the `MASTER_KEY` in your `.env` file locally.
> - On production (Azure/AWS), store it in a **Secret Manager** or **Key Vault**.
> - NEVER hardcode the key in your Python files.

### 5. Verified Trade-offs
1.  **Searchability**: You can no longer use `SELECT * FROM employee WHERE name = 'John'`. You must fetch the encrypted data and decrypt it in memory, OR use a "Blind Index" (a separate hashed column) if you need fast equality searches.
2.  **AI Vectors**: As noted in the schema, the **embedding** vectors **must remain plaintext** so that the database can perform similarity math. However, the `text_content` (the actual knowledge) is fully encrypted.
