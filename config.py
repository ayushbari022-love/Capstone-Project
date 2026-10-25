"""Project settings. Edit the DB values to match your MySQL Workbench connection."""
import os

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "ayush@2202"),   # <-- change this
    "database": os.getenv("DB_NAME", "college_events"),
}

SECRET_KEY = os.getenv("SECRET_KEY", "change-this-before-you-present")

# Students can only register with an address that ends with this domain
COLLEGE_DOMAIN = "sjcem.edu.in"

# Created automatically the first time the app starts (if no admin exists)
DEFAULT_ADMIN = {"username": "admin", "password": "Admin@123"}

# File uploads: admin's payment QR codes, and students' payment proof screenshots
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
QR_FOLDER = os.path.join(UPLOAD_FOLDER, "qr")
PROOF_FOLDER = os.path.join(UPLOAD_FOLDER, "payments")
ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "webp"}
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB per upload

# Team events: admin can require any team size in this range
MIN_TEAM_SIZE = 2
MAX_TEAM_SIZE = 6
