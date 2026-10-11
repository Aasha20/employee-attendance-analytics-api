# Employee Attendance & Analytics API

A REST API built with Python, FastAPI, and MongoDB for employee management, attendance punch-in/punch-out, manual corrections with an audit trail, and attendance analytics.

## Project Files

- `app/main.py` — API implementation
- `requirements.txt` — Python dependencies
- `openapi.yaml` — API contract and business rules
- `DATA_MODEL.md` — MongoDB data model
- `sample_data/` — Sample employee and attendance records
- `sample_seed.py` — Sample data loading script
- `REVIEW.md` — Implementation review
- `DECISIONS.md` — Design decisions and trade-offs

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer

## Setup

### 1. Create a virtual environment

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS:**

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 3. Configure MongoDB

Start MongoDB locally or configure a MongoDB Atlas cluster. Create a `.env` file in the project root containing your own connection settings:

```text
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Update these values if your MongoDB configuration differs. Do not commit `.env` files, passwords, or secrets.

### 4. Load sample data

```bash
python sample_seed.py
```

Run this when you want to load the supplied sample records into your configured database.

### 5. Start the API

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/docs to explore the API using Swagger UI.

## Submission

This public repository contains the API implementation, dependencies, supporting documentation, sample data, review notes, and design decisions. The assignment problem statement is intentionally excluded from this repository.