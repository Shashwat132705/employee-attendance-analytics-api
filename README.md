# Employee Attendance & Analytics API

A REST API for employee management, attendance tracking, and attendance analytics, built with FastAPI and MongoDB.

## Tech Stack

- Python
- FastAPI
- MongoDB
- PyMongo

## Features

- Employee and attendance data management
- Attendance records with date and status filters
- Monthly employee attendance summaries
- Department-wise attendance analytics
- Late-arrival leaderboard
- Department attendance trends
- MongoDB indexes and query explain endpoints

## Prerequisites

- Python 3.10 or newer
- MongoDB 6.0 or newer

## Setup (Windows)

1. Clone this repository:

   ```bat
   git clone https://github.com/Shashwat132705/employee-attendance-analytics-api.git
   cd employee-attendance-analytics-api
   ```

2. Create and activate a virtual environment:

   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   ```

3. Install dependencies:

   ```bat
   python -m pip install -r requirements.txt
   ```

4. Ensure MongoDB is running and configure the MongoDB connection settings expected by the application.

5. Start the API:

   ```bat
   python -m uvicorn app.main:app --port 8000
   ```

## API Documentation

When the server is running, open:

- Swagger UI: http://127.0.0.1:8000/docs
- OpenAPI documentation: http://127.0.0.1:8000/openapi.json

## Project Structure

- `app/main.py` — API implementation
- `requirements.txt` — Python dependencies
- `sample_data/` — Sample employee and attendance data
- `sample_seed.py` — Sample data loading script
- `DATA_MODEL.md` — Data model documentation
- `REVIEW.md` — Implementation review
- `DECISIONS.md` — Design decisions
- `openapi.yaml` — API contract

## Notes

Use sample data for local testing. Configure the MongoDB connection according to the application's settings before starting the API.