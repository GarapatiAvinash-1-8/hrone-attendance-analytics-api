# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (the contract mentions MongoDB 7.0 for grading)

## Setup
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your environment. For local MongoDB, the defaults are `mongodb://localhost:27017` and `attendance_db`. Do not commit credentials or `.env` files.

Seed the supplied sample records if desired:
```bash
python sample_seed.py
```

Run from the repository root:
```bash
uvicorn app.main:app --port 8000
```

Interactive API docs are available at `http://127.0.0.1:8000/docs`.

## Included
- Employee create/list and attendance punch-in, punch-out, list and regularization
- Monthly employee summary, department summary, late leaderboard, department trend, and explain endpoint
- Startup indexes, UTC/epoch-millisecond conversion, and correction audit history
- `openapi.yaml`, `DATA_MODEL.md`, and sample data from the candidate kit

## Important
This is a candidate implementation, not a claim of verified acceptance. Run it against MongoDB 7 and test it against every rule in `openapi.yaml`, especially edge cases, aggregation output, and explain plans, before submitting. Understand the implementation and revise `REVIEW.md` / `DECISIONS.md` in your own words. The grader uses a larger hidden dataset than the sample.

## GitHub submission checklist
- Commit `app/main.py`, `requirements.txt`, `README.md`, `REVIEW.md`, and `DECISIONS.md` plus relevant kit files.
- Do not commit `.env`, credentials, virtual environments, or a Dockerfile.
- Make the repository public and submit its URL through the form in the email.
