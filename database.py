import sqlite3
import os

DB = "skillgap.db"

# ── DB helper function ───────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # USERS: student/teacher roles, bio, github_username, avatar
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT UNIQUE,
        password TEXT,
        role TEXT,
        bio TEXT,
        github_username TEXT,
        avatar TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # SKILLS: predefined skill list with categories/weights
    cur.execute("""
    CREATE TABLE IF NOT EXISTS skills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        category TEXT,
        weight INTEGER DEFAULT 1
    )
    """)

    # USER_SKILLS: current skill scores
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_skills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        skill_id INTEGER,
        score REAL,
        source TEXT, -- 'resume', 'github', 'manual'
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, skill_id),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(skill_id) REFERENCES skills(id)
    )
    """)

    # SKILL_HISTORY: track score progress over time
    cur.execute("""
    CREATE TABLE IF NOT EXISTS skill_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        skill_id INTEGER,
        score REAL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(skill_id) REFERENCES skills(id)
    )
    """)

    # ROADMAPS: career path roadmaps
    cur.execute("""
    CREATE TABLE IF NOT EXISTS roadmaps(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        target_role TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    # ROADMAP_STEPS: individual tasks in a roadmap
    cur.execute("""
    CREATE TABLE IF NOT EXISTS roadmap_steps(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roadmap_id INTEGER,
        phase TEXT, -- 'Foundations', 'Intermediate', 'Advanced', 'Industry'
        title TEXT,
        description TEXT,
        resources TEXT,
        status TEXT DEFAULT 'pending', -- 'pending', 'completed'
        FOREIGN KEY(roadmap_id) REFERENCES roadmaps(id)
    )
    """)

    # MENTORSHIP_REQUESTS: student goals, project ideas
    cur.execute("""
    CREATE TABLE IF NOT EXISTS mentorship_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        goal_role TEXT,
        project_idea TEXT,
        status TEXT DEFAULT 'pending', -- 'pending', 'matched', 'completed'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(student_id) REFERENCES users(id)
    )
    """)

    # MENTORSHIP_GROUPS: teacher-led groups
    cur.execute("""
    CREATE TABLE IF NOT EXISTS mentorship_groups(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER,
        name TEXT,
        description TEXT,
        capacity INTEGER DEFAULT 5,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(teacher_id) REFERENCES users(id)
    )
    """)

    # GROUP_MEMBERS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS group_members(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER,
        student_id INTEGER,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(group_id) REFERENCES mentorship_groups(id),
        FOREIGN KEY(student_id) REFERENCES users(id)
    )
    """)

    # MESSAGES
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER,
        receiver_id INTEGER, -- Can be User ID or Group ID
        is_group_msg INTEGER DEFAULT 0,
        message TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(sender_id) REFERENCES users(id)
    )
    """)

    # Collaborative Learning tables - Phase 4 Enhanced
    cur.execute("""
    CREATE TABLE IF NOT EXISTS student_groups(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        project_title TEXT,
        project_id INTEGER,
        goal TEXT,
        description TEXT,
        status TEXT DEFAULT 'active', -- 'active', 'inactive', 'completed'
        leader_id INTEGER,
        max_members INTEGER DEFAULT 5,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(leader_id) REFERENCES users(id),
        FOREIGN KEY(project_id) REFERENCES projects(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS student_group_members(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER,
        student_id INTEGER,
        role TEXT DEFAULT 'member', -- 'leader', 'member'
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, student_id),
        FOREIGN KEY(group_id) REFERENCES student_groups(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES users(id)
    )
    """)

    # Group Invites system
    cur.execute("""
    CREATE TABLE IF NOT EXISTS group_invites(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER,
        inviter_id INTEGER,
        invitee_id INTEGER,
        status TEXT DEFAULT 'pending', -- 'pending', 'accepted', 'rejected'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(group_id) REFERENCES student_groups(id) ON DELETE CASCADE,
        FOREIGN KEY(inviter_id) REFERENCES users(id),
        FOREIGN KEY(invitee_id) REFERENCES users(id),
        UNIQUE(group_id, invitee_id)
    )
    """)

    # Group Join Requests (students requesting to join open groups)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS group_join_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER,
        student_id INTEGER,
        message TEXT,
        status TEXT DEFAULT 'pending', -- 'pending', 'accepted', 'rejected'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(group_id) REFERENCES student_groups(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES users(id),
        UNIQUE(group_id, student_id)
    )
    """)

    # SDP Proposals: Senior Design Project proposals submitted by students
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sdp_proposals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        objectives TEXT, -- JSON array of objectives
        technologies TEXT, -- JSON array of technologies
        team_size INTEGER DEFAULT 1,
        duration TEXT, -- e.g., "6 months", "1 year"
        submitted_by INTEGER,
        status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(submitted_by) REFERENCES users(id)
    )
    """)

    # Add unique index for active SDP proposals (one pending per student)
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS unique_active_proposal 
    ON sdp_proposals(submitted_by) 
    WHERE status = 'pending'
    """)

    # Teacher Group Evaluations (shortlist, high potential marks)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS project_updates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER,
        student_id INTEGER,
        message TEXT,
        update_type TEXT DEFAULT 'general', -- 'general', 'milestone', 'issue'
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(group_id) REFERENCES student_groups(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS student_availability(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE,
        status TEXT DEFAULT 'available', -- 'available', 'busy', 'open_to_collaborate'
        looking_for TEXT, -- 'project', 'group', 'mentorship'
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    # User analysis cache for matching and project recommendations
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_analysis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        matched_skills TEXT,        -- JSON
        missing_skills TEXT,        -- JSON
        match_score REAL,
        analysis_data TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    DELETE FROM user_analysis
    WHERE id NOT IN (
        SELECT MAX(id)
        FROM user_analysis
        GROUP BY user_id
    )
    """)

    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_analysis_user_id
    ON user_analysis(user_id)
    """)

    # Analytics: User analysis history for skill progress tracking
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_analysis_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        total_score INTEGER,
        skill_breakdown TEXT, -- JSON: {"frontend": 70, "backend": 40, "dsa": 60, ...}
        matched_skills TEXT, -- JSON array of matched skills
        missing_skills TEXT, -- JSON array of missing skills
        analysis_source TEXT, -- 'resume', 'github', 'combined'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_user_analysis_history_user_created
    ON user_analysis_history(user_id, created_at)
    """)

    # PROJECTS: faculty posted projects with required skills
    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        faculty_id INTEGER,
        title TEXT NOT NULL,
        description TEXT,
        required_skills TEXT,
        posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'open',
        FOREIGN KEY(faculty_id) REFERENCES users(id)
    )
    """)

    # APPLICATIONS: student applications to projects with match data
    cur.execute("""
    CREATE TABLE IF NOT EXISTS applications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        student_id INTEGER,
        student_name TEXT,
        student_skills TEXT,
        project_idea TEXT,
        interest_statement TEXT,
        match_score INTEGER DEFAULT 0,
        match_reason TEXT,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'pending',
        FOREIGN KEY(project_id) REFERENCES projects(id),
        FOREIGN KEY(student_id) REFERENCES users(id),
        UNIQUE(project_id, student_id)
    )
    """)

    initial_skills = [
        ('Python', 'Backend', 3), ('JavaScript', 'Frontend', 2), ('React', 'Frontend', 3),
        ('Node.js', 'Backend', 3), ('SQL', 'Database', 2), ('Machine Learning', 'AI/ML', 4),
        ('Docker', 'DevOps', 3), ('Git', 'Tools', 1), ('HTML', 'Frontend', 1),
        ('CSS', 'Frontend', 1), ('Flask', 'Backend', 2), ('Django', 'Backend', 3),
        ('Pandas', 'Data Science', 2), ('TypeScript', 'Frontend', 2), ('Next.js', 'Frontend', 3),
        ('Tailwind', 'Frontend', 1), ('Redis', 'Backend', 2), ('Kubernetes', 'DevOps', 4),
        ('AWS', 'DevOps', 3), ('TensorFlow', 'AI/ML', 4), ('Scikit-Learn', 'AI/ML', 3)
    ]
    cur.executemany("INSERT OR IGNORE INTO skills(name, category, weight) VALUES(?,?,?)", initial_skills)

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
