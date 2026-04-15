import sqlite3
import os

DB_PATH = "skillgap.db"

def migrate():
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found. Skipping.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    try:
        # Check if submitted_by already exists
        cur.execute("PRAGMA table_info(sdp_proposals)")
        columns = [row[1] for row in cur.fetchall()]
        
        if 'submitted_by' in columns:
            print("Migration already applied: 'submitted_by' exists in sdp_proposals.")
            return

        print("Starting sdp_proposals migration: student_id -> submitted_by")

        # Create new table
        cur.execute("""
        CREATE TABLE sdp_proposals_new(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            objectives TEXT,
            technologies TEXT,
            team_size INTEGER DEFAULT 1,
            duration TEXT,
            submitted_by INTEGER,
            status TEXT DEFAULT 'pending',
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(submitted_by) REFERENCES users(id)
        )
        """)

        # Copy data
        # Mapping student_id -> submitted_by
        if 'student_id' in columns:
            cur.execute("""
            INSERT INTO sdp_proposals_new (
                id, title, description, objectives, technologies, 
                team_size, duration, submitted_by, status, submitted_at
            )
            SELECT 
                id, title, description, objectives, technologies, 
                team_size, duration, student_id, status, submitted_at
            FROM sdp_proposals
            """)
        else:
            # Table might be empty or in weird state
            print("Warning: student_id column not found. Creating empty new table.")

        # Drop old table
        cur.execute("DROP TABLE sdp_proposals")

        # Rename new table
        cur.execute("ALTER TABLE sdp_proposals_new RENAME TO sdp_proposals")

        # Recreate the unique index
        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS unique_active_proposal 
        ON sdp_proposals(submitted_by) 
        WHERE status = 'pending'
        """)

        conn.commit()
        print("Migration successful: student_id -> submitted_by")

    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
