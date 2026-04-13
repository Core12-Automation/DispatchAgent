"""
run.py

Entry point for the DispatchAgent portal.

    python run.py

Then open http://localhost:5000
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    print()
    print("  DispatchAgent Portal")
    print("  http://localhost:5000")
    print()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
