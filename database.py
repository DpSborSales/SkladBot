import psycopg2
from psycopg2.extras import RealDictCursor

def get_db_connection():
    from config import DATABASE_URL
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
