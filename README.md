# s3upload

A simple, generic S3 directory sync tool. Upload a local folder to S3 and download it back anywhere else — structure preserved, duplicates skipped.

No framework. No config file. One script, one dependency.

---

## What it does

```
python s3upload.py --upload ./my-folder
python s3upload.py --download ./my-folder
```

- Uploads/downloads a full directory tree to/from S3
- Skips files that already exist (safe to run repeatedly)
- Preserves folder structure exactly
- Supports any S3-compatible storage (Backblaze B2, Cloudflare R2, MinIO, etc.)

---

## Requirements

- Python 3.10+
- `boto3`

```bash
pip install -r requirements.txt
```

---

## Usage

### Upload a directory

```bash
python s3upload.py --upload ./my-folder
```

Uploads `./my-folder/` and everything inside it to S3, preserving structure:

```
./my-folder/a/b.txt  →  s3://your-bucket/my-folder/a/b.txt
```

### Download a directory

```bash
python s3upload.py --download ./my-folder
```

Downloads everything under `s3://your-bucket/my-folder/` into `./my-folder/`.

### Options

| Flag | Description |
|------|-------------|
| `--upload <dir>` | Upload local directory to S3 |
| `--download <dir>` | Download from S3 into local directory |
| `--overwrite` | Overwrite files that already exist |
| `--dry-run` | Show what would happen without doing anything |
| `--include-hidden` | Include hidden files and directories (`.dotfiles`) |

### Dry run first

```bash
python s3upload.py --upload ./my-folder --dry-run
```

Always a good idea before a large upload.

---

## Environment variables

Set these before running the script.

### Required

```bash
export AWS_ACCESS_KEY_ID="your-access-key-id"
export AWS_SECRET_ACCESS_KEY="your-secret-access-key"
export AWS_DEFAULT_REGION="us-east-1"
export S3_BUCKET="your-bucket-name"
```

### Optional

```bash
# Adds a prefix to every S3 key
# ./my-folder/file.txt → s3://bucket/private/my-folder/file.txt
export S3_PREFIX="private"

# For S3-compatible storage (Backblaze B2, Cloudflare R2, MinIO, etc.)
export S3_ENDPOINT_URL="https://s3.us-west-004.backblazeb2.com"
```

---

## Output

```
UPLOAD               my-folder/checkpoints/model.safetensors
SKIP already exists  my-folder/lora/style.safetensors
UPLOAD               my-folder/embeddings/token.pt

Uploaded: 2  Skipped: 1  Errors: 0
```

---

## Files skipped automatically

Unless `--include-hidden` is passed:

- `.DS_Store`
- `Thumbs.db`
- `__pycache__`
- `*.pyc` / `*.pyo`
- Any file or directory starting with `.`

---

## Setting up an S3 bucket (step by step)

> This guide uses AWS S3. For Backblaze B2 or Cloudflare R2, see the sections further below.

### Step 1 — Create an AWS account

Go to [aws.amazon.com](https://aws.amazon.com) and sign up if you do not have an account.

---

### Step 2 — Create an S3 bucket

1. Open the [S3 console](https://s3.console.aws.amazon.com/s3/)
2. Click **Create bucket**
3. Fill in:
   - **Bucket name** — must be globally unique, e.g. `yourname-models-2024`
   - **Region** — pick one close to you, e.g. `us-east-1`
4. Under **Block Public Access settings**:
   - Leave **all boxes checked** (block everything). This bucket is private.
5. Leave everything else as default
6. Click **Create bucket**

Note your bucket name and region. You will need them.

---

### Step 3 — Create an IAM user

S3 access requires an IAM user with an access key. Do **not** use your root account credentials.

1. Open the [IAM console](https://console.aws.amazon.com/iam/)
2. In the left sidebar, click **Users** → **Create user**
3. **User name** — e.g. `s3upload-user`
4. Click **Next**
5. On the permissions page, select **Attach policies directly**
6. Search for and select **AmazonS3FullAccess**
   - If you want tighter permissions, see [Minimal IAM policy](#minimal-iam-policy) below
7. Click **Next** → **Create user**

---

### Step 4 — Create an access key

1. Click the user you just created
2. Go to the **Security credentials** tab
3. Scroll to **Access keys** → click **Create access key**
4. Select **Other** as the use case → click **Next**
5. Click **Create access key**
6. **Copy both values now** — the secret key is only shown once:
   - Access key ID
   - Secret access key

---

### Step 5 — Set environment variables

On your local machine or remote instance:

```bash
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="abc123..."
export AWS_DEFAULT_REGION="us-east-1"
export S3_BUCKET="yourname-models-2024"
```

To make these permanent, add them to your shell profile (`~/.zshrc`, `~/.bashrc`):

```bash
echo 'export AWS_ACCESS_KEY_ID="AKIA..."' >> ~/.zshrc
echo 'export AWS_SECRET_ACCESS_KEY="abc123..."' >> ~/.zshrc
echo 'export AWS_DEFAULT_REGION="us-east-1"' >> ~/.zshrc
echo 'export S3_BUCKET="yourname-models-2024"' >> ~/.zshrc
source ~/.zshrc
```

---

### Step 6 — Run the script

```bash
pip install -r requirements.txt

# Upload
python s3upload.py --upload ./my-folder --dry-run
python s3upload.py --upload ./my-folder

# Download (on another machine)
python s3upload.py --download ./my-folder
```

---

## Minimal IAM policy

`AmazonS3FullAccess` is broad. If you want to restrict this user to one bucket only, create a custom policy instead.

In the IAM console, under **Policies** → **Create policy** → **JSON**, paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:HeadObject"
      ],
      "Resource": [
        "arn:aws:s3:::yourname-models-2024",
        "arn:aws:s3:::yourname-models-2024/*"
      ]
    }
  ]
}
```

Replace `yourname-models-2024` with your actual bucket name.

This policy allows:
- Uploading files
- Downloading files
- Listing the bucket contents
- Checking if a file exists

It does **not** allow deleting files or accessing any other bucket.

---

## Using with Backblaze B2

Backblaze B2 is S3-compatible and significantly cheaper than AWS for storage and egress.

### Setup

1. Create a [Backblaze account](https://www.backblaze.com/b2/cloud-storage.html)
2. Go to **Buckets** → **Create a Bucket**
   - Set **Files in Bucket are** → **Private**
3. Go to **App Keys** → **Add a New Application Key**
   - Select your bucket
   - Enable **Read and Write**
   - Copy the **keyID** and **applicationKey**
4. Find your endpoint in the bucket details — it looks like:
   `s3.us-west-004.backblazeb2.com`

### Environment variables

```bash
export AWS_ACCESS_KEY_ID="your-keyID"
export AWS_SECRET_ACCESS_KEY="your-applicationKey"
export AWS_DEFAULT_REGION="us-west-004"
export S3_BUCKET="your-bucket-name"
export S3_ENDPOINT_URL="https://s3.us-west-004.backblazeb2.com"
```

---

## Using with Cloudflare R2

Cloudflare R2 has no egress fees, making it good for frequent downloads.

### Setup

1. In the Cloudflare dashboard, go to **R2** → **Create bucket**
2. Go to **R2** → **Manage R2 API Tokens** → **Create API Token**
   - Permission: **Object Read & Write**
   - Scope to your bucket
   - Copy the **Access Key ID** and **Secret Access Key**
3. Your endpoint is: `https://<account-id>.r2.cloudflarestorage.com`
   - Find your account ID in the right sidebar of the Cloudflare dashboard

### Environment variables

```bash
export AWS_ACCESS_KEY_ID="your-r2-access-key-id"
export AWS_SECRET_ACCESS_KEY="your-r2-secret-access-key"
export AWS_DEFAULT_REGION="auto"
export S3_BUCKET="your-bucket-name"
export S3_ENDPOINT_URL="https://<account-id>.r2.cloudflarestorage.com"
```

---

## Cost estimate (AWS S3)

For reference, storing 100 GB of model files on AWS S3 in `us-east-1`:

| Item | Cost |
|------|------|
| Storage (100 GB/month) | ~$2.30/month |
| PUT requests (upload, 10k files) | ~$0.05 one-time |
| GET requests (download, 10k files) | ~$0.004 one-time |
| Data transfer out (100 GB) | ~$9.00 one-time |

Backblaze B2 storage is ~$0.006/GB/month with 3× cheaper egress.
Cloudflare R2 has no egress fees.

---

## License

MIT
