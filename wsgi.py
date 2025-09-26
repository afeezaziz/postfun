from app import create_app

app = create_app()

if __name__ == "__main__":
    # For local dev convenience; prefer gunicorn in production
    app.run(host="0.0.0.0", port=8000, debug=True)
