# City Bus Portal - Multi-City Bus Transport Management
import os
import time
import hmac
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse
from typing import Optional, Tuple

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
import streamlit as st
from psycopg2 import Error, extensions

SUPPORTED_CITIES = ["Ahmedabad", "Baroda", "Surat", "Rajkot", "Gandhinagar", "Bhavnagar"]
DEFAULT_CITY = "Ahmedabad"
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCK_MINUTES = 10

# Keep admin password configurable but preserve existing behavior for legacy setups.
DEFAULT_ADMIN_PASSWORD = os.getenv("CITYBUS_DEFAULT_ADMIN_PASSWORD", "admin123")
ADMIN_ACCESS_CODE = os.getenv("CITYBUS_ADMIN_ACCESS_CODE", "789641").strip()

def _build_db_config() -> dict:
    """Build DB config from Streamlit secrets, DATABASE_URL, or env vars."""
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        parsed = urlparse(db_url)
        if parsed.scheme.startswith("postgres"):
            return {
                "host": parsed.hostname or "",
                "database": (parsed.path or "").lstrip("/"),
                "user": parsed.username or "",
                "password": parsed.password or "",
                "port": parsed.port or 5432,
                "sslmode": os.getenv("CITYBUS_DB_SSLMODE", "require"),
            }

    # Streamlit Cloud secrets.toml support:
    # [database]
    # host = "..."
    # name = "..."
    # user = "..."
    # password = "..."
    # port = 5432
    # sslmode = "require"
    try:
        if "database" in st.secrets:
            secret_db = st.secrets["database"]
            return {
                "host": secret_db.get("host", ""),
                "database": secret_db.get("name", secret_db.get("database", "")),
                "user": secret_db.get("user", ""),
                "password": secret_db.get("password", ""),
                "port": int(secret_db.get("port", 5432)),
                "sslmode": secret_db.get("sslmode", os.getenv("CITYBUS_DB_SSLMODE", "require")),
            }
    except Exception:
        pass

    return {
        "host": os.getenv("CITYBUS_DB_HOST", "localhost"),
        "database": os.getenv("CITYBUS_DB_NAME", "brts_db"),
        "user": os.getenv("CITYBUS_DB_USER", "postgres"),
        "password": os.getenv("CITYBUS_DB_PASSWORD", ""),
        "port": int(os.getenv("CITYBUS_DB_PORT", "5432")),
        "sslmode": os.getenv("CITYBUS_DB_SSLMODE", "prefer"),
    }


# Database Configuration
DB_CONFIG = _build_db_config()

# Database Connection
@st.cache_resource
def _create_connection():
    return psycopg2.connect(**DB_CONFIG)


def get_connection():
    """Get a healthy shared database connection."""
    try:
        conn = _create_connection()

        # Recreate closed/stale cached connections.
        if conn.closed != 0:
            _create_connection.clear()
            conn = _create_connection()

        # Recover from aborted transactions that would otherwise trigger
        # InFailedSqlTransaction on the next query.
        if conn.get_transaction_status() == extensions.TRANSACTION_STATUS_INERROR:
            conn.rollback()

        return conn
    except Error as e:
        st.error(f"Database connection error: {e}")
        if DB_CONFIG.get("host") in {"localhost", "127.0.0.1", "::1"}:
            st.info(
                "Set a remote PostgreSQL in Streamlit Secrets or DATABASE_URL. "
                "Localhost does not work on Streamlit Cloud."
            )
        return None


# Initialize Database Schema
def init_database():
    """Initialize database tables"""
    conn = get_connection()
    if not conn:
        return False

    cursor = conn.cursor()
    try:
        # Clear failed transaction state on shared cached connections.
        if conn.get_transaction_status() == extensions.TRANSACTION_STATUS_INERROR:
            conn.rollback()

        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password VARCHAR(256) NOT NULL,
                role VARCHAR(20) NOT NULL,
                full_name VARCHAR(200),
                email VARCHAR(100),
                phone VARCHAR(15),
                preferred_city VARCHAR(100) DEFAULT 'Ahmedabad',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Buses table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS buses (
                bus_id SERIAL PRIMARY KEY,
                bus_number VARCHAR(50) UNIQUE NOT NULL,
                capacity INTEGER NOT NULL,
                bus_type VARCHAR(50),
                city VARCHAR(100) DEFAULT 'Ahmedabad',
                status VARCHAR(20) DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Routes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                route_id SERIAL PRIMARY KEY,
                route_name VARCHAR(200) NOT NULL,
                source VARCHAR(200) NOT NULL,
                destination VARCHAR(200) NOT NULL,
                distance_km DECIMAL(10,2),
                fare DECIMAL(10,2),
                city VARCHAR(100) DEFAULT 'Ahmedabad',
                status VARCHAR(20) DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Stops table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stops (
                stop_id SERIAL PRIMARY KEY,
                route_id INTEGER REFERENCES routes(route_id),
                stop_name VARCHAR(200) NOT NULL,
                stop_order INTEGER NOT NULL,
                arrival_time TIME
            )
        """)

        # Schedules table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                schedule_id SERIAL PRIMARY KEY,
                bus_id INTEGER REFERENCES buses(bus_id),
                route_id INTEGER REFERENCES routes(route_id),
                departure_time TIME NOT NULL,
                arrival_time TIME NOT NULL,
                days_of_week VARCHAR(50),
                status VARCHAR(20) DEFAULT 'active'
            )
        """)

        # Bookings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                booking_id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(user_id),
                schedule_id INTEGER REFERENCES schedules(schedule_id),
                booking_date DATE NOT NULL,
                num_passengers INTEGER DEFAULT 1,
                total_fare DECIMAL(10,2),
                payment_status VARCHAR(20) DEFAULT 'pending',
                booking_status VARCHAR(20) DEFAULT 'confirmed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Payments table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                payment_id SERIAL PRIMARY KEY,
                booking_id INTEGER REFERENCES bookings(booking_id),
                amount DECIMAL(10,2) NOT NULL,
                payment_method VARCHAR(50),
                transaction_id VARCHAR(100),
                payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status VARCHAR(20) DEFAULT 'completed'
            )
        """)

        # Support Tickets table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(user_id),
                subject VARCHAR(200) NOT NULL,
                message TEXT NOT NULL,
                admin_reply TEXT,
                status VARCHAR(20) DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feedback table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                feedback_id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(user_id),
                rating INTEGER CHECK (rating >= 1 AND rating <= 5),
                comments TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Persist base schema before best-effort migrations.
        conn.commit()

        # Add missing columns to bookings if they don't exist.
        try:
            cursor.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS source_stop_id INTEGER REFERENCES stops(stop_id)")
            cursor.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS dest_stop_id INTEGER REFERENCES stops(stop_id)")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_city VARCHAR(100) DEFAULT 'Ahmedabad'")
            cursor.execute("ALTER TABLE buses ADD COLUMN IF NOT EXISTS city VARCHAR(100) DEFAULT 'Ahmedabad'")
            cursor.execute("ALTER TABLE routes ADD COLUMN IF NOT EXISTS city VARCHAR(100) DEFAULT 'Ahmedabad'")
            cursor.execute("UPDATE users SET preferred_city=%s WHERE preferred_city IS NULL OR preferred_city=''", (DEFAULT_CITY,))
            cursor.execute("UPDATE buses SET city=%s WHERE city IS NULL OR city=''", (DEFAULT_CITY,))
            cursor.execute("UPDATE routes SET city=%s WHERE city IS NULL OR city=''", (DEFAULT_CITY,))
            # Fix for days_of_week length.
            cursor.execute("ALTER TABLE schedules ALTER COLUMN days_of_week TYPE VARCHAR(100)")
            conn.commit()
        except Exception:
            conn.rollback()

        # Insert default admin if not exists.
        cursor.execute("""
            INSERT INTO users (username, password, role, full_name, email, preferred_city)
            VALUES ('admin', %s, 'admin', 'System Administrator', 'admin@citybus.com', %s)
            ON CONFLICT (username) DO NOTHING
        """, (hash_password(DEFAULT_ADMIN_PASSWORD), DEFAULT_CITY))

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        st.error(f"Database initialization error: {e}")
        return False
    finally:
        cursor.close()


# Utility Functions
def hash_password(password: str) -> str:
    """Hash password using SHA-256"""
    return hashlib.sha256(password.encode()).hexdigest()


def city_index(city: Optional[str]) -> int:
    if city in SUPPORTED_CITIES:
        return SUPPORTED_CITIES.index(city)
    return 0


def preferred_city_for_user(user: Optional[dict]) -> str:
    if user and user.get("preferred_city") in SUPPORTED_CITIES:
        return user["preferred_city"]
    return DEFAULT_CITY


def logout_user():
    st.session_state.logged_in = False
    st.session_state.user = None
    st.session_state.admin_verified = False
    st.session_state.pending_admin_user = None
    st.session_state.auth_page = 'login'


def verify_admin_session() -> Tuple[bool, str]:
    """Server-side guard to ensure only verified admins can access admin pages."""
    user = st.session_state.get("user")
    if not user:
        return False, "No active user session found."
    if user.get("role") != "admin":
        return False, "Only administrators can access the admin panel."
    if not st.session_state.get("admin_verified", False):
        return False, "Admin session is not verified."

    conn = get_connection()
    if not conn:
        return False, "Unable to verify admin session due to database connectivity."

    cursor = conn.cursor()
    cursor.execute(
        "SELECT role FROM users WHERE user_id=%s AND username=%s",
        (user.get("user_id"), user.get("username"))
    )
    db_user = cursor.fetchone()
    cursor.close()

    if not db_user or db_user[0] != "admin":
        return False, "Admin privileges could not be verified."
    return True, ""


def verify_user(
    login_id: str,
    password: str,
    admin_access_code: str = "",
    require_admin_code: bool = True
) -> Tuple[Optional[dict], Optional[str]]:
    """Verify credentials by username/email and optionally enforce admin access code."""
    conn = get_connection()
    if not conn:
        return None, "Database is unavailable. Please try again."

    cursor = conn.cursor()
    hashed_pwd = hash_password(password)
    cursor.execute("""
        SELECT user_id, username, role, full_name, email, COALESCE(preferred_city, %s)
        FROM users
        WHERE (username=%s OR LOWER(email)=LOWER(%s)) AND password=%s
    """, (DEFAULT_CITY, login_id, login_id, hashed_pwd))
    result = cursor.fetchone()
    cursor.close()

    if not result:
        return None, "Invalid username/email or password."

    user = {
        'user_id': result[0],
        'username': result[1],
        'role': result[2],
        'full_name': result[3],
        'email': result[4],
        'preferred_city': result[5] if result[5] in SUPPORTED_CITIES else DEFAULT_CITY
    }

    if user["role"] == "admin" and require_admin_code:
        if not ADMIN_ACCESS_CODE:
            return None, "Admin access code is not configured. Set CITYBUS_ADMIN_ACCESS_CODE in environment."
        if not admin_access_code:
            return None, "Admin access code is required for administrator login."
        if not hmac.compare_digest(admin_access_code.strip(), ADMIN_ACCESS_CODE):
            return None, "Invalid admin access code."

    return user, None


def init_page():
    """Initialize page configuration and styles"""
    if 'theme' not in st.session_state:
        st.session_state.theme = 'dark'

    st.set_page_config(
        page_title="City Bus Portal",
        page_icon="🚌",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    # Theme Colors
    if st.session_state.theme == 'dark':
        # Dark Mode
        colors = {
            "bg_color": "#1e1e2f",
            "bg_gradient": "radial-gradient(at 0% 0%, hsla(253,16%,7%,1) 0, transparent 50%), radial-gradient(at 50% 0%, hsla(225,39%,30%,1) 0, transparent 50%), radial-gradient(at 100% 0%, hsla(339,49%,30%,1) 0, transparent 50%)",
            "card_bg": "rgba(255, 255, 255, 0.05)",
            "card_border": "rgba(255, 255, 255, 0.1)",
            "text_color": "#ffffff",
            "text_secondary": "rgba(255, 255, 255, 0.7)",
            "input_bg": "rgba(255, 255, 255, 0.05)",
            "header_gradient": "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
            "sidebar_bg": "linear-gradient(180deg, #0f1729 0%, #1a1f3a 100%)",
            "metric_bg": "rgba(255, 255, 255, 0.03)"
        }
    else:
        # Light Mode
        colors = {
            "bg_color": "#f0f2f5",
            "bg_gradient": "linear-gradient(120deg, #fdfbfb 0%, #ebedee 100%)",
            "card_bg": "rgba(255, 255, 255, 0.8)",
            "card_border": "rgba(0, 0, 0, 0.05)",
            "text_color": "#1a1a2e",
            "text_secondary": "rgba(0, 0, 0, 0.6)",
            "input_bg": "rgba(255, 255, 255, 0.9)",
            "header_gradient": "linear-gradient(135deg, #667eea 0%, #764ba2 100%)", # Keep header colorful
            "sidebar_bg": "#ffffff",
            "metric_bg": "rgba(255, 255, 255, 0.6)"
        }

    st.markdown(f"""
        <style>
        :root {{
            --bg-color: {colors['bg_color']};
            --card-bg: {colors['card_bg']};
            --text-color: {colors['text_color']};
            --text-secondary: {colors['text_secondary']};
            --input-bg: {colors['input_bg']};
        }}

        /* Global App Background */
        .stApp {{
            background-color: {colors['bg_color']};
            background-image: {colors['bg_gradient']};
            color: {colors['text_color']};
            transition: all 0.5s ease;
        }}

        /* Cards */
        .content-card, .stat-card, .booking-confirmed-card {{
            background: {colors['card_bg']};
            border: 1px solid {colors['card_border']};
            border-radius: 16px; 
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.05);
            backdrop-filter: blur(8.5px);
            -webkit-backdrop-filter: blur(8.5px);
            padding: 25px;
            margin-bottom: 20px;
            color: {colors['text_color']};
        }}
        
        /* Typography overrides */
        h1, h2, h3, h4, h5, h6, .auth-subtitle, .stat-label, p, label {{
            color: {colors['text_color']} !important;
        }}
        .auth-subtitle, .stat-label, small, .caption {{
             color: {colors['text_secondary']} !important;
        }}

        /* Dashboard Header */
        .dash-header {{
            background: {colors['header_gradient']};
            padding: 30px;
            border-radius: 20px;
            margin-bottom: 30px;
            box-shadow: 0 10px 20px rgba(0,0,0,0.1);
            color: white !important;
        }}
        .dash-header h1, .dash-header p {{
            color: white !important;
        }}

        /* Sidebar */
        section[data-testid="stSidebar"] {{
            background: {colors['sidebar_bg']} !important;
            border-right: 1px solid {colors['card_border']};
        }}
        
        /* Inputs - Targeting the container to include the eye button and icons */
        [data-testid="stTextInput"] > div, [data-testid="stNumberInput"] > div, 
        [data-testid="stDateInput"] > div, [data-testid="stTimeInput"] > div, 
        [data-testid="stTextArea"] > div, 
        div[data-baseweb="select"] > div {{
            background-color: {colors['input_bg']} !important;
            color: {colors['text_color']} !important;
            border-radius: 10px;
            border: 1px solid {colors['card_border']} !important;
        }}
        
        /* Ensure inner elements are transparent and fit well */
        .stTextInput input, .stNumberInput input, .stDateInput input, .stTimeInput input, .stTextArea textarea {{
            background-color: transparent !important;
            border: none !important;
            padding: 8px 12px !important;
        }}
        
        /* Remove right-side gap near password eye icon */
        div[data-baseweb="input"] {{
            padding-right: 0 !important;
        }}
        div[data-baseweb="input"] > div {{
            padding-right: 0 !important;
            background: transparent !important;
        }}
        div[data-baseweb="input"] button {{
            border: none !important;
            margin: 0 !important;
            padding: 0 8px !important;
            min-width: auto !important;
            background: transparent !important;
        }}
        div[data-baseweb="input"] button:hover {{
            background: transparent !important;
        }}

        /* Hide "Press Enter to submit form" helper text */
        [data-testid="stFormInviteText"],
        [data-testid="InputInstructions"] {{
            display: none !important;
        }}

        /* Metrics */
        [data-testid="stMetric"] {{
            background: transparent;
            border: none;
            color: {colors['text_color']};
        }}
        [data-testid="stMetricLabel"] {{
            color: {colors['text_secondary']};
        }}
        [data-testid="stMetricValue"] {{
            color: {colors['text_color']};
        }}
        
        /* Buttons - Keep them popping */
        .stButton button {{
            background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
            color: white !important;
            border: none;
            border-radius: 10px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        }}
        
        /* Expander */
        .streamlit-expanderHeader {{
            color: {colors['text_color']} !important;
            background-color: transparent !important;
        }}
        
        /* Dataframes */
        [data-testid="stDataFrame"] {{
            background-color: {colors['card_bg']};
        }}
        
        /* Compact Forms */
        [data-testid="stForm"] {{
            padding: 1rem 1.5rem;
            border: 1px solid {colors['card_border']};
            border-radius: 16px;
            background: {colors['card_bg']};
        }}
        
        /* Reduce spacing between elements */
        .block-container {{
            padding-top: 3.5rem;
            padding-bottom: 3rem;
        }}
        .element-container {{
            margin-bottom: 0.5rem;
        }}

        /* Tighter form spacing for password/input fields */
        [data-testid="stForm"] [data-testid="stTextInput"] {{
            margin-bottom: -0.5rem;
        }}

        /* Phone prefix styling */
        .phone-prefix {{
            display: flex;
            align-items: center;
            height: 42px;
            padding: 0 8px;
            font-size: 0.9rem;
            margin-top: 0;
        }}
        </style>
    """, unsafe_allow_html=True)


# Session State Initialization
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
if 'user' not in st.session_state:
    st.session_state.user = None
if 'admin_verified' not in st.session_state:
    st.session_state.admin_verified = False
if 'auth_page' not in st.session_state:
    st.session_state.auth_page = 'login'
if 'quick_nav' not in st.session_state:
    st.session_state.quick_nav = None
if 'login_attempts' not in st.session_state:
    st.session_state.login_attempts = 0
if 'login_lock_until' not in st.session_state:
    st.session_state.login_lock_until = None
if 'pending_admin_user' not in st.session_state:
    st.session_state.pending_admin_user = None


# Authentication Pages
def login_page():
    """Modern login page"""
    st.markdown("""
        <div class="auth-logo">
            <h1 class="auth-logo-text">🚌 BRTS</h1>
            <p class="auth-subtitle">Bus Rapid Transit System</p>
        </div>
    """, unsafe_allow_html=True)

    # Center the form
    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:
        st.markdown('<h2 class="auth-title">Sign in</h2>', unsafe_allow_html=True)

        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Username or email", placeholder="Enter your username", label_visibility="visible")
            password = st.text_input("Password", type="password", placeholder="Enter your password",
                                     label_visibility="visible")

            submitted = st.form_submit_button("Sign in", use_container_width=True, type="primary")

            if submitted:
                if username and password:
                    user = verify_user(username, password)
                    if user:
                        st.session_state.logged_in = True
                        st.session_state.user = user
                        st.success(f"Welcome back, {user['full_name']}!")
                        time.sleep(0.5)
                        st.rerun()
                    else:
                        st.error("⚠️ Invalid username or password")
                else:
                    st.warning("Please enter both username and password")

    

        if st.button("Create account", use_container_width=True, key="goto_register"):
            st.session_state.auth_page = 'register'
            st.rerun()

    
        st.caption("💡 Demo: Use username `admin` and password `admin123` to login as administrator")


def register_page():
    """Modern registration page"""
    st.markdown("""
        <div class="auth-logo">
            <h1 class="auth-logo-text">🚌 BRTS</h1>
            <p class="auth-subtitle">Bus Rapid Transit System</p>
        </div>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:
        st.markdown('<h2 class="auth-title">Create your account</h2>', unsafe_allow_html=True)

        with st.form("register_form", clear_on_submit=False):
            full_name = st.text_input("Full Name", placeholder="John Doe", label_visibility="visible")
            email = st.text_input("Email", placeholder="john@example.com", label_visibility="visible")
            username = st.text_input("Username", placeholder="Choose a username", label_visibility="visible")

            phone_number = st.text_input("🇮🇳 +91  Phone Number", placeholder="9876543210", max_chars=10,
                                         label_visibility="visible")

            password = st.text_input("Password", type="password", placeholder="Create a strong password",
                                     label_visibility="visible")
            confirm_password = st.text_input("Confirm Password", type="password", placeholder="Re-enter your password",
                                             label_visibility="visible")

            submitted = st.form_submit_button("Create account", use_container_width=True, type="primary")

            if submitted:
                phone = f"+91{phone_number}" if phone_number else ""
                if not all([full_name, email, username, password, confirm_password, phone_number]):
                    st.warning("Please fill in all required fields")
                elif len(phone_number) != 10 or not phone_number.isdigit():
                    st.error("⚠️ Phone number must be exactly 10 digits")
                elif password != confirm_password:
                    st.error("⚠️ Passwords don't match")
                elif len(password) < 6:
                    st.error("⚠️ Password must be at least 6 characters")
                else:
                    conn = get_connection()
                    if conn:
                        try:
                            cursor = conn.cursor()
                            cursor.execute("""
                                INSERT INTO users (username, password, role, full_name, email, phone)
                                VALUES (%s, %s, 'passenger', %s, %s, %s)
                            """, (username, hash_password(password), full_name, email, phone))
                            conn.commit()
                            cursor.close()
                            st.success("✅ Account created successfully!")
                            st.info("Redirecting to login...")
                            time.sleep(1.5)
                            st.session_state.auth_page = 'login'
                            st.rerun()
                        except Exception as e:
                            if 'duplicate key' in str(e).lower():
                                st.error("⚠️ Username already exists")
                            else:
                                st.error(f"Registration failed: {e}")

    

        if st.button("← Back to Sign in", use_container_width=True, key="goto_login"):
            st.session_state.auth_page = 'login'
            st.rerun()


# Dashboard Functions
def render_header(title, subtitle=None):
    """Render dashboard header"""
    if subtitle:
        st.markdown(f"""
            <div class="dash-header">
                <div>
                    <h1 class="dash-title">{title}</h1>
                    <p class="dash-welcome">{subtitle}</p>
                </div>
            </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown(f"""
            <div class="dash-header">
                <h1 class="dash-title">{title}</h1>
            </div>
        """, unsafe_allow_html=True)


def admin_dashboard():
    """Clean admin dashboard"""
    render_header("Dashboard", f"Welcome, {st.session_state.user['full_name']}")

    conn = get_connection()
    if conn:
        cursor = conn.cursor()

        # Get statistics
        cursor.execute("SELECT COUNT(*) FROM buses WHERE status='active'")
        total_buses = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM routes WHERE status='active'")
        total_routes = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM bookings WHERE booking_date=CURRENT_DATE")
        today_bookings = cursor.fetchone()[0]

        cursor.execute("SELECT COALESCE(SUM(total_fare), 0) FROM bookings WHERE booking_date=CURRENT_DATE")
        today_revenue = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM users WHERE role='passenger'")
        total_users = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM schedules WHERE status='active'")
        active_schedules = cursor.fetchone()[0]

        # Display stats
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown(f"""
                <div class="stat-card">
                    <div class="stat-label">📅 Today's Bookings</div>
                    <div class="stat-value">{today_bookings}</div>
                </div>
            """, unsafe_allow_html=True)

        with col2:
            st.markdown(f"""
                <div class="stat-card">
                    <div class="stat-label">💰 Today's Revenue</div>
                    <div class="stat-value">₹{float(today_revenue):.0f}</div>
                </div>
            """, unsafe_allow_html=True)

        with col3:
            st.markdown(f"""
                <div class="stat-card">
                    <div class="stat-label">👥 Total Users</div>
                    <div class="stat-value">{total_users}</div>
                </div>
            """, unsafe_allow_html=True)

    

        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown(f"""
                <div class="stat-card">
                    <div class="stat-label">🚌 Active Buses</div>
                    <div class="stat-value">{total_buses}</div>
                </div>
            """, unsafe_allow_html=True)

        with col2:
            st.markdown(f"""
                <div class="stat-card">
                    <div class="stat-label">🛣️ Active Routes</div>
                    <div class="stat-value">{total_routes}</div>
                </div>
            """, unsafe_allow_html=True)

        with col3:
            st.markdown(f"""
                <div class="stat-card">
                    <div class="stat-label">⏰ Active Schedules</div>
                    <div class="stat-value">{active_schedules}</div>
                </div>
            """, unsafe_allow_html=True)

    

        # Quick Actions
        st.markdown('<h3 class="section-title">⚡ Quick Actions</h3>', unsafe_allow_html=True)

        col1, col2, col3, col4 = st.columns(4)

        with col1:
            if st.button("📊 View Analytics", use_container_width=True, type="primary"):
                st.session_state.quick_nav = "Analytics"
                st.rerun()

        with col2:
            if st.button("📋 View Bookings", use_container_width=True):
                st.session_state.quick_nav = "Bookings"
                st.rerun()

        with col3:
            if st.button("🚌 Manage Buses", use_container_width=True):
                st.session_state.quick_nav = "Buses"
                st.rerun()

        with col4:
            if st.button("📈 View Reports", use_container_width=True):
                st.session_state.quick_nav = "Reports"
                st.rerun()


        # Charts
        col1, col2 = st.columns(2)

        with col1:
            st.markdown('<h3 class="section-title">Weekly Bookings Trend</h3>', unsafe_allow_html=True)
            cursor.execute("""
                SELECT booking_date, COUNT(*) as count
                FROM bookings
                WHERE booking_date >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY booking_date
                ORDER BY booking_date
            """)
            weekly_data = cursor.fetchall()
            if weekly_data:
                df = pd.DataFrame(weekly_data, columns=['Date', 'Bookings'])
                fig = px.area(df, x='Date', y='Bookings', color_discrete_sequence=['#667eea'])
                fig.update_layout(height=300, margin={'l': 0, 'r': 0, 't': 0, 'b': 0})
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No booking data available")

        with col2:
            st.markdown('<h3 class="section-title">Revenue Overview</h3>', unsafe_allow_html=True)
            cursor.execute("""
                SELECT booking_date, COALESCE(SUM(total_fare), 0) as revenue
                FROM bookings
                WHERE booking_date >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY booking_date
                ORDER BY booking_date
            """)
            revenue_data = cursor.fetchall()
            if revenue_data:
                df = pd.DataFrame(revenue_data, columns=['Date', 'Revenue'])
                fig = px.bar(df, x='Date', y='Revenue', color_discrete_sequence=['#764ba2'])
                fig.update_layout(height=300, margin={'l': 0, 'r': 0, 't': 0, 'b': 0})
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No revenue data available")

        # Recent Bookings
        st.markdown('<h3 class="section-title">Recent Bookings (Last 5)</h3>', unsafe_allow_html=True)

        cursor.execute("""
            SELECT b.booking_id, u.full_name, r.route_name, b.booking_date, 
                   b.total_fare, b.booking_status
            FROM bookings b
            JOIN users u ON b.user_id = u.user_id
            JOIN schedules s ON b.schedule_id = s.schedule_id
            JOIN routes r ON s.route_id = r.route_id
            ORDER BY b.created_at DESC
            LIMIT 5
        """)

        recent_bookings = cursor.fetchall()

        if recent_bookings:
            df_recent = pd.DataFrame(recent_bookings,
                                     columns=['ID', 'Passenger', 'Route', 'Date', 'Fare', 'Status'])
            st.dataframe(df_recent, use_container_width=True, hide_index=True)
        else:
            st.info("No recent bookings")




        # System Maintenance
        st.markdown('<h3 class="section-title">⚠️ System Maintenance</h3>', unsafe_allow_html=True)
        
        with st.expander("Reset System Stats", expanded=False):
            st.warning("This will delete ALL bookings, payments, and support tickets. Routes, Buses, and Users will remain.")
            confirm_reset = st.checkbox("I confirm that I want to reset all statistical data")
            
            if st.button("Reset All Stats", type="primary", disabled=not confirm_reset):
                try:
                    # Truncate tables that hold transactional data
                    cursor.execute("TRUNCATE bookings, payments, support_tickets RESTART IDENTITY CASCADE")
                    conn.commit()
                    st.success("System stats reset successfully!")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

        cursor.close()


def manage_buses():
    """Comprehensive bus management with CRUD"""
    render_header("Bus Management")

    tab1, tab2, tab3, tab4 = st.tabs(["📋 View All", "➕ Add Bus", "✏️ Edit Bus", "🗑️ Delete Bus"])

    conn = get_connection()

    with tab1:
        cursor = conn.cursor()
        cursor.execute("SELECT bus_id, bus_number, capacity, bus_type, status FROM buses ORDER BY bus_id DESC")
        buses = cursor.fetchall()

        if buses:
            df = pd.DataFrame(buses, columns=['ID', 'Bus Number', 'Capacity', 'Type', 'Status'])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No buses found. Add your first bus using the 'Add Bus' tab.")
        cursor.close()

    with tab2:
        col1, col2 = st.columns(2)
        with col1:
            bus_number = st.text_input("Bus Number", placeholder="e.g., BR-101")
            bus_type = st.selectbox("Bus Type", ["Standard", "AC", "Luxury", "Electric"], key="add_bus_type")
        with col2:
            capacity = st.number_input("Capacity", min_value=10, max_value=100, value=40, key="add_bus_cap")
            status = st.selectbox("Status", ["active", "inactive", "maintenance"], key="add_bus_status")

        if st.button("Add Bus", type="primary", use_container_width=True):
            if bus_number:
                try:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO buses (bus_number, capacity, bus_type, status)
                        VALUES (%s, %s, %s, %s)
                    """, (bus_number, capacity, bus_type, status))
                    conn.commit()
                    cursor.close()
                    st.success(f"✅ Bus {bus_number} added successfully!")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
            else:
                st.warning("Please enter bus number")

    with tab3:
        cursor = conn.cursor()
        cursor.execute("SELECT bus_id, bus_number, capacity, bus_type, status FROM buses ORDER BY bus_number")
        all_buses = cursor.fetchall()

        if not all_buses:
            st.info("No buses to edit.")
        else:
            bus_edit_options = {f"{b[1]} (ID: {b[0]})": b for b in all_buses}
            selected_edit = st.selectbox("Select Bus to Edit", list(bus_edit_options.keys()), key="edit_bus_sel")
            bus_data = bus_edit_options[selected_edit]

            with st.form("edit_bus_form"):
                col1, col2 = st.columns(2)
                with col1:
                    edit_bus_number = st.text_input("Bus Number", value=bus_data[1])
                    type_options = ["Standard", "AC", "Luxury", "Electric"]
                    current_type_idx = type_options.index(bus_data[3]) if bus_data[3] in type_options else 0
                    edit_bus_type = st.selectbox("Bus Type", type_options, index=current_type_idx)
                with col2:
                    edit_capacity = st.number_input("Capacity", min_value=10, max_value=100, value=bus_data[2])
                    status_options = ["active", "inactive", "maintenance"]
                    current_status_idx = status_options.index(bus_data[4]) if bus_data[4] in status_options else 0
                    edit_status = st.selectbox("Status", status_options, index=current_status_idx)

                if st.form_submit_button("Update Bus", type="primary", use_container_width=True):
                    try:
                        cursor.execute("""
                            UPDATE buses SET bus_number=%s, capacity=%s, bus_type=%s, status=%s
                            WHERE bus_id=%s
                        """, (edit_bus_number, edit_capacity, edit_bus_type, edit_status, bus_data[0]))
                        conn.commit()
                        st.success(f"✅ Bus '{edit_bus_number}' updated!")
                        time.sleep(1)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")
        cursor.close()

    with tab4:
        cursor = conn.cursor()
        cursor.execute("SELECT bus_id, bus_number, capacity, bus_type, status FROM buses ORDER BY bus_number")
        del_buses = cursor.fetchall()

        if not del_buses:
            st.info("No buses to delete.")
        else:
            bus_del_options = {f"{b[1]} (ID: {b[0]}) - {b[4]}": b[0] for b in del_buses}
            selected_del = st.selectbox("Select Bus to Delete", list(bus_del_options.keys()), key="del_bus_sel")
            bus_del_id = bus_del_options[selected_del]

            st.warning("⚠️ Deleting a bus will also remove all its linked schedules and may affect bookings.")
            confirm_del = st.checkbox("I confirm I want to delete this bus", key="confirm_del_bus")

            if st.button("🗑️ Delete Bus", type="primary", use_container_width=True, disabled=not confirm_del):
                try:
                    # Delete related schedules first
                    cursor.execute("DELETE FROM schedules WHERE bus_id=%s", (bus_del_id,))
                    cursor.execute("DELETE FROM buses WHERE bus_id=%s", (bus_del_id,))
                    conn.commit()
                    st.success("✅ Bus deleted successfully!")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
        cursor.close()



def manage_routes():
    """Clean route management with stops support"""
    render_header("Route Management")

    tab1, tab2, tab3, tab4 = st.tabs(["📋 Routes", "➕ Add Route", "✏️ Edit Route", "🗑️ Delete Route"])

    conn = get_connection()
    if not conn:
        return
        
    cursor = conn.cursor()

    with tab1:
        # View Existing Routes
        cursor.execute("SELECT route_id, route_name, source, destination, distance_km, fare, status FROM routes ORDER BY route_id")
        routes = cursor.fetchall()

        if routes:
            df = pd.DataFrame(routes, columns=['ID', 'Name', 'Source', 'Destination', 'Distance (km)', 'Fare', 'Status'])
            st.dataframe(df, use_container_width=True, hide_index=True)

            # Show stops per route
            st.divider()
            st.subheader("Stops by Route")
            route_stop_options = {f"{r[1]}": r[0] for r in routes}
            view_route_name = st.selectbox("Select Route", list(route_stop_options.keys()), key="view_stops_route")
            view_route_id = route_stop_options[view_route_name]

            cursor.execute("""
                SELECT stop_id, stop_name, stop_order, arrival_time 
                FROM stops WHERE route_id=%s ORDER BY stop_order
            """, (view_route_id,))
            route_stops = cursor.fetchall()

            if route_stops:
                stops_df = pd.DataFrame(route_stops, columns=['ID', 'Stop Name', 'Order', 'Arrival Time'])
                st.dataframe(stops_df, use_container_width=True, hide_index=True)
            else:
                st.info("No stops defined for this route.")
        else:
            st.info("No routes found. Add your first route.")

    with tab2:
        st.subheader("Add New Route")
        col1, col2 = st.columns(2)
        with col1:
            route_name = st.text_input("Route Name", placeholder="e.g. Route 101")
            source = st.text_input("Source", placeholder="Starting Point")
            destination = st.text_input("Destination", placeholder="End Point")
        with col2:
            distance = st.number_input("Distance (km)", min_value=0.1, step=0.1)
            fare = st.number_input("Fare (₹)", min_value=1.0, step=0.5)

        if st.button("Add Route", type="primary", use_container_width=True):
            if route_name and source and destination:
                try:
                    cursor.execute("""
                        INSERT INTO routes (route_name, source, destination, distance_km, fare)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING route_id
                    """, (route_name, source, destination, distance, fare))
                    new_route_id = cursor.fetchone()[0]
                    
                    # Auto-create source and destination as stops
                    cursor.execute("""
                        INSERT INTO stops (route_id, stop_name, stop_order, arrival_time)
                        VALUES (%s, %s, 1, '00:00'), (%s, %s, 2, '00:00')
                    """, (new_route_id, source, new_route_id, destination))
                    
                    conn.commit()
                    st.success(f"✅ Route '{route_name}' added with default stops ({source} → {destination})!")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
            else:
                st.warning("Please fill in all required fields")

        # Add Stop to existing route
        st.divider()
        st.subheader("Add Stop to Route")
        cursor.execute("SELECT route_id, route_name FROM routes WHERE status='active' ORDER BY route_name")
        active_routes_add = cursor.fetchall()

        if active_routes_add:
            add_stop_route_opts = {f"{r[1]}": r[0] for r in active_routes_add}
            sel_route_for_stop = st.selectbox("Select Route", list(add_stop_route_opts.keys()), key="add_stop_route")
            sel_route_id = add_stop_route_opts[sel_route_for_stop]

            # Get existing stops for order suggestion
            cursor.execute("SELECT stop_order FROM stops WHERE route_id=%s ORDER BY stop_order DESC LIMIT 1", (sel_route_id,))
            last_order = cursor.fetchone()
            next_order = (last_order[0] + 1) if last_order else 1

            col1, col2, col3 = st.columns(3)
            with col1:
                stop_name = st.text_input("Stop Name", placeholder="e.g. Central Station", key="new_stop_name")
            with col2:
                stop_order = st.number_input("Stop Order", min_value=1, value=next_order, step=1, key="new_stop_order")
            with col3:
                default_time = datetime.strptime('09:00', '%H:%M').time()
                arrival_time_input = st.time_input("Approx. Arrival Time", value=default_time, key="new_stop_time")

            if st.button("Add Stop", type="primary", use_container_width=True):
                if stop_name:
                    try:
                        cursor.execute("""
                            INSERT INTO stops (route_id, stop_name, stop_order, arrival_time)
                            VALUES (%s, %s, %s, %s)
                        """, (sel_route_id, stop_name, stop_order, arrival_time_input))
                        conn.commit()
                        st.success(f"✅ Stop '{stop_name}' added!")
                        time.sleep(0.5)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error adding stop: {e}")
                else:
                    st.warning("Please enter a stop name")
        else:
            st.info("Add an active route first.")

    with tab3:
        st.subheader("Edit Route")
        cursor.execute("SELECT route_id, route_name, source, destination, distance_km, fare, status FROM routes ORDER BY route_name")
        edit_routes = cursor.fetchall()

        if not edit_routes:
            st.info("No routes to edit.")
        else:
            edit_route_opts = {f"{r[1]} (ID: {r[0]})": r for r in edit_routes}
            sel_edit_route = st.selectbox("Select Route to Edit", list(edit_route_opts.keys()), key="edit_route_sel")
            route_data = edit_route_opts[sel_edit_route]

            with st.form("edit_route_form"):
                col1, col2 = st.columns(2)
                with col1:
                    edit_route_name = st.text_input("Route Name", value=route_data[1])
                    edit_source = st.text_input("Source", value=route_data[2])
                    edit_destination = st.text_input("Destination", value=route_data[3])
                with col2:
                    edit_distance = st.number_input("Distance (km)", min_value=0.1, step=0.1, value=float(route_data[4]))
                    edit_fare = st.number_input("Fare (₹)", min_value=1.0, step=0.5, value=float(route_data[5]))
                    status_opts = ["active", "inactive"]
                    current_idx = status_opts.index(route_data[6]) if route_data[6] in status_opts else 0
                    edit_route_status = st.selectbox("Status", status_opts, index=current_idx)

                if st.form_submit_button("Update Route", type="primary", use_container_width=True):
                    try:
                        cursor.execute("""
                            UPDATE routes SET route_name=%s, source=%s, destination=%s, 
                            distance_km=%s, fare=%s, status=%s WHERE route_id=%s
                        """, (edit_route_name, edit_source, edit_destination, 
                              edit_distance, edit_fare, edit_route_status, route_data[0]))
                        conn.commit()
                        st.success(f"✅ Route '{edit_route_name}' updated!")
                        time.sleep(1)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

            # Edit Stops for this route
            st.divider()
            st.subheader(f"Edit Stops for {route_data[1]}")
            cursor.execute("""
                SELECT stop_id, stop_name, stop_order, arrival_time 
                FROM stops WHERE route_id=%s ORDER BY stop_order
            """, (route_data[0],))
            edit_stops = cursor.fetchall()

            if edit_stops:
                for s in edit_stops:
                    with st.expander(f"Stop #{s[2]}: {s[1]}"):
                        with st.form(f"edit_stop_{s[0]}"):
                            e_col1, e_col2, e_col3 = st.columns(3)
                            with e_col1:
                                e_stop_name = st.text_input("Name", value=s[1], key=f"esn_{s[0]}")
                            with e_col2:
                                e_stop_order = st.number_input("Order", min_value=1, value=s[2], key=f"eso_{s[0]}")
                            with e_col3:
                                try:
                                    e_arrival = s[3] if s[3] else datetime.strptime('09:00', '%H:%M').time()
                                except:
                                    e_arrival = datetime.strptime('09:00', '%H:%M').time()
                                e_arrival_time = st.time_input("Arrival", value=e_arrival, key=f"eat_{s[0]}")
                            
                            sc1, sc2 = st.columns(2)
                            with sc1:
                                if st.form_submit_button("💾 Save", use_container_width=True):
                                    try:
                                        cursor.execute("""
                                            UPDATE stops SET stop_name=%s, stop_order=%s, arrival_time=%s
                                            WHERE stop_id=%s
                                        """, (e_stop_name, e_stop_order, e_arrival_time, s[0]))
                                        conn.commit()
                                        st.success(f"✅ Stop updated!")
                                        time.sleep(0.5)
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Error: {e}")

                # Delete individual stops
                st.divider()
                st.subheader("Delete a Stop")
                del_stop_opts = {f"#{s[2]}: {s[1]} (ID: {s[0]})": s[0] for s in edit_stops}
                sel_del_stop = st.selectbox("Select Stop to Delete", list(del_stop_opts.keys()), key="del_stop_sel")
                del_stop_id = del_stop_opts[sel_del_stop]
                
                if st.button("🗑️ Delete This Stop", type="primary", use_container_width=True):
                    try:
                        cursor.execute("DELETE FROM stops WHERE stop_id=%s", (del_stop_id,))
                        conn.commit()
                        st.success("✅ Stop deleted!")
                        time.sleep(0.5)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")
            else:
                st.info("No stops for this route.")

    with tab4:
        st.subheader("Delete Route")
        cursor.execute("SELECT route_id, route_name, source, destination, status FROM routes ORDER BY route_name")
        del_routes = cursor.fetchall()

        if not del_routes:
            st.info("No routes to delete.")
        else:
            del_route_opts = {f"{r[1]} ({r[2]} → {r[3]}) - {r[4]}": r[0] for r in del_routes}
            sel_del_route = st.selectbox("Select Route to Delete", list(del_route_opts.keys()), key="del_route_sel")
            del_route_id = del_route_opts[sel_del_route]

            st.warning("⚠️ Deleting a route will also remove all its stops and linked schedules.")
            confirm_del_route = st.checkbox("I confirm I want to delete this route", key="confirm_del_route")

            if st.button("🗑️ Delete Route", type="primary", use_container_width=True, disabled=not confirm_del_route):
                try:
                    cursor.execute("DELETE FROM stops WHERE route_id=%s", (del_route_id,))
                    cursor.execute("DELETE FROM schedules WHERE route_id=%s", (del_route_id,))
                    cursor.execute("DELETE FROM routes WHERE route_id=%s", (del_route_id,))
                    conn.commit()
                    st.success("✅ Route and all linked data deleted!")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

    cursor.close()


def manage_schedules():
    """Clean schedule management"""
    render_header("Schedule Management")

    tab1, tab2 = st.tabs(["View All Schedules", "Add New Schedule"])

    conn = get_connection()

    with tab1:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.schedule_id, b.bus_number, r.route_name, 
                   s.departure_time, s.arrival_time, s.days_of_week, s.status
            FROM schedules s
            JOIN buses b ON s.bus_id = b.bus_id
            JOIN routes r ON s.route_id = r.route_id
            ORDER BY s.schedule_id DESC
        """)
        schedules = cursor.fetchall()

        if schedules:
            df = pd.DataFrame(schedules, columns=['ID', 'Bus', 'Route', 'Departure', 'Arrival', 'Days', 'Status'])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No schedules found. Add your first schedule using the 'Add New Schedule' tab.")
        cursor.close()

    with tab2:

        cursor = conn.cursor()

        cursor.execute("SELECT bus_id, bus_number FROM buses WHERE status='active'")
        buses = cursor.fetchall()

        cursor.execute("SELECT route_id, route_name FROM routes WHERE status='active'")
        routes = cursor.fetchall()

        if not buses or not routes:
            st.warning("Please add active buses and routes first")
            cursor.close()
            return

        col1, col2 = st.columns(2)

        with col1:
            bus_options = {f"{b[1]}": b[0] for b in buses}
            selected_bus = st.selectbox("Select Bus", list(bus_options.keys()))
            bus_id = bus_options[selected_bus]

            departure_time = st.time_input("Departure Time")

        with col2:
            route_options = {f"{r[1]}": r[0] for r in routes}
            selected_route = st.selectbox("Select Route", list(route_options.keys()))
            route_id = route_options[selected_route]

            arrival_time = st.time_input("Arrival Time")

        days_of_week = st.multiselect(
            "Operating Days",
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
            default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        )

    

        if st.button("Add Schedule", type="primary", use_container_width=True):
            if days_of_week:
                try:
                    days_str = ",".join(days_of_week)
                    cursor.execute("""
                        INSERT INTO schedules (bus_id, route_id, departure_time, arrival_time, days_of_week)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (bus_id, route_id, departure_time, arrival_time, days_str))
                    conn.commit()
                    st.success("✅ Schedule added successfully!")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
            else:
                st.warning("Please select at least one day")

        cursor.close()



def check_user_ticket_limit(user_id):
    """Check if user has reached ticket limit (max 5 active bookings)"""
    conn = get_connection()
    if not conn: return False
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM bookings 
        WHERE user_id = %s AND booking_status = 'confirmed' 
        AND booking_date >= CURRENT_DATE
    """, (user_id,))
    count = cursor.fetchone()[0]
    cursor.close()
    return count < 5

def check_segment_availability(schedule_id, booking_date, start_order, end_order, passengers):
    """Check availability for specific segment"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get bus capacity
    cursor.execute("""
        SELECT b.capacity, r.route_id
        FROM schedules s
        JOIN buses b ON s.bus_id = b.bus_id
        JOIN routes r ON s.route_id = r.route_id
        WHERE s.schedule_id = %s
    """, (schedule_id,))
    res = cursor.fetchone()
    if not res:
        cursor.close()
        return False, 0, 0
    
    capacity, route_id = res
    
    # Get all active bookings for this schedule/date
    cursor.execute("""
        SELECT b.num_passengers, s1.stop_order, s2.stop_order
        FROM bookings b
        LEFT JOIN stops s1 ON b.source_stop_id = s1.stop_id
        LEFT JOIN stops s2 ON b.dest_stop_id = s2.stop_id
        WHERE b.schedule_id = %s 
        AND b.booking_date = %s 
        AND b.booking_status = 'confirmed'
    """, (schedule_id, booking_date))
    
    current_bookings = cursor.fetchall()
    
    # Get route limits to handle legacy bookings
    cursor.execute("""
        SELECT MIN(stop_order), MAX(stop_order)
        FROM stops WHERE route_id = %s
    """, (route_id,))
    stops_range = cursor.fetchone()
    min_stop = stops_range[0] if stops_range[0] else 1
    max_stop = stops_range[1] if stops_range[1] else 100
    
    # Check each segment in requested range
    # A segment is the travel between stop i and i+1
    # If I travel from stop 2 to 5, I occupy segments 2->3, 3->4, 4->5
    # i ranges from start_order to end_order - 1
    
    max_segment_load = 0
    
    for i in range(start_order, end_order):
        current_load = 0
        for bk_passengers, bk_start, bk_end in current_bookings:
            # Handle legacy/full route bookings
            s = bk_start if bk_start else min_stop
            e = bk_end if bk_end else max_stop
            
            # Check if booking covers this segment (i -> i+1)
            # Booking interval [s, e) covers segment [i, i+1) if s <= i and e > i
            if s <= i and e > i:
                current_load += bk_passengers
                
        if current_load + passengers > capacity:
            cursor.close()
            return False, current_load, capacity
        
        max_segment_load = max(max_segment_load, current_load)
            
    cursor.close()
    return True, max_segment_load, capacity

def booking_system():
    """Segment-based booking interface"""
    render_header("Book Your Ticket")


    conn = get_connection()
    cursor = conn.cursor()

    # Step 1: Select Route
    cursor.execute("SELECT route_id, route_name, source, destination, fare, distance_km FROM routes WHERE status='active'")
    routes = cursor.fetchall()

    if not routes:
        st.warning("No routes available currently.")
        cursor.close()
        return

    route_options = {f"{r[1]} ({r[2]} → {r[3]})": r for r in routes}
    selected_route_key = st.selectbox("Select Route", list(route_options.keys()))
    route_data = route_options[selected_route_key]
    route_id, route_name, route_source, route_dest, route_fare, route_dist = route_data

    # Step 2: Select Stops
    cursor.execute("SELECT stop_id, stop_name, stop_order FROM stops WHERE route_id=%s ORDER BY stop_order", (route_id,))
    stops = cursor.fetchall()

    if not stops or len(stops) < 2:
        st.warning("This route doesn't have enough stops defined yet.")
        cursor.close()
        return

    col1, col2 = st.columns(2)
    
    stop_options = {s[1]: (s[0], s[2]) for s in stops} # Name -> (ID, Order)
    stop_names = [s[1] for s in stops]

    with col1:
        source_stop_name = st.selectbox("From Stop", stop_names)
        source_id, source_order = stop_options[source_stop_name]

    with col2:
        # Filter dest stops to be after source
        dest_options = [s[1] for s in stops if s[2] > source_order]
        if not dest_options:
            st.error("Select a valid source stop")
            return
            
        dest_stop_name = st.selectbox("To Stop", dest_options)
        dest_id, dest_order = stop_options[dest_stop_name]

    # Step 3: Date & Passengers
    col_d, col_p = st.columns(2)
    with col_d:
        booking_date = st.date_input("Journey Date", min_value=datetime.now().date())
    with col_p:
        num_passengers = st.number_input("Number of Passengers", min_value=1, max_value=5, value=1, help="Max 5 tickets per booking")

    # Step 4: Select Schedule
    cursor.execute("""
        SELECT s.schedule_id, b.bus_number, s.departure_time, s.arrival_time, b.bus_type
        FROM schedules s
        JOIN buses b ON s.bus_id = b.bus_id
        WHERE s.route_id=%s AND s.status='active'
    """, (route_id,))
    schedules = cursor.fetchall()

    if not schedules:
        st.warning("No buses scheduled for this route.")
        cursor.close()
        return

    # Filter schedules by day of week
    day_name = booking_date.strftime("%A")
    valid_schedules = []
    
    for sch in schedules:
        # Check actual availability
        is_avail, load, cap = check_segment_availability(sch[0], booking_date, source_order, dest_order, num_passengers)
        if is_avail:
             valid_schedules.append(sch)

    if not valid_schedules:
        st.error(f"No seats available for {num_passengers} passengers on this date (or no buses running).")
        cursor.close()
        return

    schedule_options = {f"{s[1]} ({s[4]}) - Departs {s[2]}": s[0] for s in valid_schedules}
    selected_schedule_key = st.selectbox("Select Bus", list(schedule_options.keys()))
    schedule_id = schedule_options[selected_schedule_key]

    # Step 5: Calculate Fare
    # Fare logic: (Total Fare / Total Route Distance) * Segment Distance
    # Since we don't have exact segment distance, we'll use stop proportion or just a simple fraction
    # Approximation: (Stop Diff / Total Stops) * Base Fare
    total_stops_count = len(stops)
    stops_travelled = dest_order - source_order
    
    # Avoid division by zero/weirdness
    if total_stops_count > 1:
        calculated_fare = (float(route_fare) / (total_stops_count - 1)) * stops_travelled
    else:
        calculated_fare = float(route_fare)
        
    calculated_fare = max(5.0, round(calculated_fare)) # Minimum fare 5
    total_amount = calculated_fare * num_passengers

    st.markdown('<div class="info-box">', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    c1.metric("Fare per Seat", f"₹{calculated_fare}")
    c2.metric("Passengers", num_passengers)
    c3.metric("Total Payable", f"₹{total_amount}")

    payment_method = st.selectbox("Payment Method", ["UPI", "Credit Card", "Debit Card", "Net Banking"])
    

    
    # Check user limit before showing button
    if not check_user_ticket_limit(st.session_state.user['user_id']):
        st.error("🚫 You have reached the maximum limit of 5 active bookings.")
    else:
        if st.button("Confirm Booking & Pay", type="primary", use_container_width=True):
            try:
                # Double check availability (race condition)
                is_avail, _, _ = check_segment_availability(schedule_id, booking_date, source_order, dest_order, num_passengers)
                if not is_avail:
                    st.error("Sorry! Seats were just booked by someone else.")
                else:
                    cursor.execute("""
                        INSERT INTO bookings (user_id, schedule_id, booking_date, num_passengers, total_fare, 
                                            payment_status, source_stop_id, dest_stop_id)
                        VALUES (%s, %s, %s, %s, %s, 'completed', %s, %s)
                        RETURNING booking_id
                    """, (st.session_state.user['user_id'], schedule_id, booking_date, num_passengers, 
                          total_amount, source_id, dest_id))

                    booking_id = cursor.fetchone()[0]

                    transaction_id = f"TXN{booking_id}{int(time.time())}"
                    cursor.execute("""
                        INSERT INTO payments (booking_id, amount, payment_method, transaction_id)
                        VALUES (%s, %s, %s, %s)
                    """, (booking_id, total_amount, payment_method, transaction_id))

                    conn.commit()

                    st.toast("🎉 Booking confirmed!", icon="✅")
                    st.balloons()

                    st.markdown(f"""
                        <div class="booking-confirmed-card">
                            <h3>🎫 Booking Confirmed!</h3>
                            <p><strong>Booking ID:</strong> {booking_id}</p>
                            <p><strong>Journey:</strong> {source_stop_name} ➝ {dest_stop_name}</p>
                            <p><strong>Date:</strong> {booking_date}</p>
                            <p><strong>Passengers:</strong> {num_passengers}</p>
                            <p><strong>Total Paid:</strong> ₹{total_amount}</p>
                        </div>
                    """, unsafe_allow_html=True)

            except Exception as e:
                st.error(f"Booking failed: {e}")

    cursor.close()



def analytics_dashboard():
    """Comprehensive analytics dashboard for admin"""
    render_header("Analytics Dashboard")

    conn = get_connection()
    cursor = conn.cursor()

    # Date range selector
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        start_date = st.date_input("From Date", value=datetime.now().date() - timedelta(days=30))
    with col2:
        end_date = st.date_input("To Date", value=datetime.now().date())
    with col3:
    
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    # Overview Metrics
    st.markdown('<h3 class="section-title">📊 Overview Metrics</h3>', unsafe_allow_html=True)

    # Get key metrics
    cursor.execute("""
        SELECT 
            COUNT(*) as total_bookings,
            COALESCE(SUM(total_fare), 0) as total_revenue,
            COALESCE(SUM(num_passengers), 0) as total_passengers,
            COUNT(DISTINCT user_id) as unique_users
        FROM bookings
        WHERE booking_date BETWEEN %s AND %s
    """, (start_date, end_date))

    metrics = cursor.fetchone()
    total_bookings, total_revenue, total_passengers, unique_users = metrics

    # Calculate averages
    days_diff = (end_date - start_date).days + 1
    avg_daily_bookings = total_bookings / days_diff if days_diff > 0 else 0
    avg_booking_value = total_revenue / total_bookings if total_bookings > 0 else 0

    col1, col2, col3, col4, col5, col6 = st.columns(6)

    with col1:
        st.markdown(f"""
            <div class="stat-card">
                <div class="stat-label">Total Bookings</div>
                <div class="stat-value">{total_bookings}</div>
            </div>
        """, unsafe_allow_html=True)

    with col2:
        st.markdown(f"""
            <div class="stat-card">
                <div class="stat-label">Total Revenue</div>
                <div class="stat-value">₹{float(total_revenue):,.0f}</div>
            </div>
        """, unsafe_allow_html=True)

    with col3:
        st.markdown(f"""
            <div class="stat-card">
                <div class="stat-label">Total Passengers</div>
                <div class="stat-value">{total_passengers}</div>
            </div>
        """, unsafe_allow_html=True)

    with col4:
        st.markdown(f"""
            <div class="stat-card">
                <div class="stat-label">Unique Users</div>
                <div class="stat-value">{unique_users}</div>
            </div>
        """, unsafe_allow_html=True)

    with col5:
        st.markdown(f"""
            <div class="stat-card">
                <div class="stat-label">Avg Daily Bookings</div>
                <div class="stat-value">{avg_daily_bookings:.1f}</div>
            </div>
        """, unsafe_allow_html=True)

    with col6:
        st.markdown(f"""
            <div class="stat-card">
                <div class="stat-label">Avg Booking Value</div>
                <div class="stat-value">₹{float(avg_booking_value):.0f}</div>
            </div>
        """, unsafe_allow_html=True)


    # Booking Trends
    st.markdown('<h3 class="section-title">📈 Booking & Revenue Trends</h3>', unsafe_allow_html=True)

    cursor.execute("""
        SELECT 
            booking_date,
            COUNT(*) as bookings,
            SUM(total_fare) as revenue,
            SUM(num_passengers) as passengers
        FROM bookings
        WHERE booking_date BETWEEN %s AND %s
        GROUP BY booking_date
        ORDER BY booking_date
    """, (start_date, end_date))

    trends_data = cursor.fetchall()

    if trends_data:
        df_trends = pd.DataFrame(trends_data, columns=['Date', 'Bookings', 'Revenue', 'Passengers'])

        # Create dual-axis chart
        fig = go.Figure()

        fig.add_trace(go.Scatter(
            x=df_trends['Date'], y=df_trends['Bookings'],
            name='Bookings', mode='lines+markers',
            line={'color': '#667eea', 'width': 3},
            yaxis='y'
        ))

        fig.add_trace(go.Scatter(
            x=df_trends['Date'], y=df_trends['Revenue'],
            name='Revenue (₹)', mode='lines+markers',
            line={'color': '#764ba2', 'width': 3},
            yaxis='y2'
        ))

        fig.update_layout(
            xaxis={'title': 'Date'},
            yaxis={'title': {'text': 'Number of Bookings', 'font': {'color': '#667eea'}}},
            yaxis2={'title': {'text': 'Revenue (INR)', 'font': {'color': '#764ba2'}}, 'overlaying': 'y', 'side': 'right'},
            hovermode='x unified',
            height=400
        )

        st.plotly_chart(fig, use_container_width=True)

        # Show trend stats
        col1, col2, col3 = st.columns(3)
        with col1:
            growth = ((df_trends['Bookings'].iloc[-1] - df_trends['Bookings'].iloc[0]) / df_trends['Bookings'].iloc[
                0] * 100) if len(df_trends) > 1 and df_trends['Bookings'].iloc[0] > 0 else 0
            st.metric("Booking Growth", f"{growth:.1f}%", delta=f"{growth:.1f}%")
        with col2:
            st.metric("Peak Day Bookings", int(df_trends['Bookings'].max()))
        with col3:
            st.metric("Peak Day Revenue", f"₹{df_trends['Revenue'].max():,.0f}")
    else:
        st.info("No booking data available for selected period")


    # Route Performance Analysis
    col1, col2 = st.columns(2)

    with col1:
        st.markdown('<h3 class="section-title">🏆 Top Routes by Bookings</h3>', unsafe_allow_html=True)

        cursor.execute("""
            SELECT 
                r.route_name,
                r.source,
                r.destination,
                COUNT(b.booking_id) as bookings,
                SUM(b.total_fare) as revenue,
                SUM(b.num_passengers) as passengers
            FROM bookings b
            JOIN schedules s ON b.schedule_id = s.schedule_id
            JOIN routes r ON s.route_id = r.route_id
            WHERE b.booking_date BETWEEN %s AND %s
            GROUP BY r.route_id, r.route_name, r.source, r.destination
            ORDER BY bookings DESC
            LIMIT 10
        """, (start_date, end_date))

        top_routes = cursor.fetchall()

        if top_routes:
            df_routes = pd.DataFrame(top_routes, columns=['Route', 'From', 'To', 'Bookings', 'Revenue', 'Passengers'])

            fig = px.bar(df_routes, x='Bookings', y='Route', orientation='h',
                         color='Revenue', color_continuous_scale='Viridis',
                         title='', height=400)
            fig.update_layout(showlegend=False, yaxis={'categoryorder': 'total ascending'})
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No route data available")


    with col2:
        st.markdown('<h3 class="section-title">💰 Revenue by Route</h3>', unsafe_allow_html=True)

        if top_routes:
            fig = px.pie(df_routes.head(7), values='Revenue', names='Route',
                         hole=0.4, height=400)
            fig.update_traces(textposition='inside', textinfo='percent+label')
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No revenue data available")


    # Peak Hours Analysis
    st.markdown('<h3 class="section-title">⏰ Peak Booking Hours</h3>', unsafe_allow_html=True)

    cursor.execute("""
        SELECT 
            EXTRACT(HOUR FROM s.departure_time) as hour,
            COUNT(b.booking_id) as bookings
        FROM bookings b
        JOIN schedules s ON b.schedule_id = s.schedule_id
        WHERE b.booking_date BETWEEN %s AND %s
        GROUP BY hour
        ORDER BY hour
    """, (start_date, end_date))

    peak_hours = cursor.fetchall()

    if peak_hours:
        df_hours = pd.DataFrame(peak_hours, columns=['Hour', 'Bookings'])
        df_hours['Hour'] = df_hours['Hour'].astype(int)
        df_hours['Time'] = df_hours['Hour'].apply(lambda x: f"{int(x):02d}:00")

        fig = px.bar(df_hours, x='Time', y='Bookings',
                     color='Bookings', color_continuous_scale='Blues',
                     title='Bookings by Departure Hour')
        fig.update_layout(showlegend=False, height=350)
        st.plotly_chart(fig, use_container_width=True)

        peak_hour = df_hours.loc[df_hours['Bookings'].idxmax()]
        st.info(f"🔥 Peak Hour: **{peak_hour['Time']}** with **{int(peak_hour['Bookings'])}** bookings")
    else:
        st.info("No peak hour data available")


    # Payment & Booking Status
    col1, col2 = st.columns(2)

    with col1:
        st.markdown('<h3 class="section-title">💳 Payment Method Distribution</h3>', unsafe_allow_html=True)

        cursor.execute("""
            SELECT 
                p.payment_method,
                COUNT(*) as count,
                SUM(p.amount) as total_amount
            FROM payments p
            JOIN bookings b ON p.booking_id = b.booking_id
            WHERE b.booking_date BETWEEN %s AND %s
            GROUP BY p.payment_method
            ORDER BY count DESC
        """, (start_date, end_date))

        payment_data = cursor.fetchall()

        if payment_data:
            df_payment = pd.DataFrame(payment_data, columns=['Method', 'Count', 'Amount'])

            fig = px.pie(df_payment, values='Count', names='Method',
                         hole=0.3, height=300,
                         color_discrete_sequence=px.colors.qualitative.Set3)
            fig.update_traces(textposition='inside', textinfo='percent+label')
            st.plotly_chart(fig, use_container_width=True)

            st.dataframe(df_payment, use_container_width=True, hide_index=True)
        else:
            st.info("No payment data available")


    with col2:
        st.markdown('<h3 class="section-title">📋 Booking Status Breakdown</h3>', unsafe_allow_html=True)

        cursor.execute("""
            SELECT 
                booking_status,
                COUNT(*) as count,
                SUM(total_fare) as revenue
            FROM bookings
            WHERE booking_date BETWEEN %s AND %s
            GROUP BY booking_status
        """, (start_date, end_date))

        status_data = cursor.fetchall()

        if status_data:
            df_status = pd.DataFrame(status_data, columns=['Status', 'Count', 'Revenue'])

            fig = px.bar(df_status, x='Status', y='Count',
                         color='Status', height=300,
                         color_discrete_map={'confirmed': '#28a745', 'cancelled': '#dc3545'})
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

            # Cancellation rate
            total = df_status['Count'].sum()
            cancelled = df_status[df_status['Status'] == 'cancelled']['Count'].sum() if 'cancelled' in df_status[
                'Status'].values else 0
            cancellation_rate = (cancelled / total * 100) if total > 0 else 0

            col_a, col_b = st.columns(2)
            with col_a:
                st.metric("Total Bookings", int(total))
            with col_b:
                st.metric("Cancellation Rate", f"{cancellation_rate:.1f}%")
        else:
            st.info("No status data available")


    # Bus Performance
    st.markdown('<h3 class="section-title">🚌 Bus Performance Metrics</h3>', unsafe_allow_html=True)

    cursor.execute("""
        SELECT 
            b.bus_number,
            b.bus_type,
            COUNT(bk.booking_id) as trips,
            SUM(bk.num_passengers) as total_passengers,
            SUM(bk.total_fare) as revenue,
            b.capacity
        FROM bookings bk
        JOIN schedules s ON bk.schedule_id = s.schedule_id
        JOIN buses b ON s.bus_id = b.bus_id
        WHERE bk.booking_date BETWEEN %s AND %s
        GROUP BY b.bus_id, b.bus_number, b.bus_type, b.capacity
        ORDER BY trips DESC
    """, (start_date, end_date))

    bus_performance = cursor.fetchall()

    if bus_performance:
        df_buses = pd.DataFrame(bus_performance,
                                columns=['Bus Number', 'Type', 'Trips', 'Passengers', 'Revenue', 'Capacity'])

        # Calculate utilization
        df_buses['Avg Occupancy'] = (df_buses['Passengers'] / df_buses['Trips']).round(1)
        df_buses['Utilization %'] = ((df_buses['Avg Occupancy'] / df_buses['Capacity']) * 100).round(1)

        # Display table
        display_df = df_buses[
            ['Bus Number', 'Type', 'Trips', 'Passengers', 'Revenue', 'Avg Occupancy', 'Utilization %']]
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        # Utilization chart
        fig = px.bar(df_buses.head(10), x='Bus Number', y='Utilization %',
                     color='Utilization %', color_continuous_scale='RdYlGn',
                     title='Bus Utilization Rate (%)')
        fig.add_hline(y=75, line_dash="dash", line_color="orange",
                      annotation_text="Target: 75%")
        fig.update_layout(height=350)
        st.plotly_chart(fig, use_container_width=True)

        # Performance summary
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Most Active Bus", df_buses.iloc[0]['Bus Number'], f"{int(df_buses.iloc[0]['Trips'])} trips")
        with col2:
            avg_util = df_buses['Utilization %'].mean()
            st.metric("Avg Fleet Utilization", f"{avg_util:.1f}%")
        with col3:
            top_revenue_bus = df_buses.loc[df_buses['Revenue'].idxmax()]
            st.metric("Top Revenue Bus", top_revenue_bus['Bus Number'], f"₹{float(top_revenue_bus['Revenue']):,.0f}")
    else:
        st.info("No bus performance data available")


    # User Engagement
    st.markdown('<h3 class="section-title">👥 User Engagement Metrics</h3>', unsafe_allow_html=True)

    cursor.execute("""
        SELECT 
            u.full_name,
            u.email,
            COUNT(b.booking_id) as total_bookings,
            SUM(b.total_fare) as total_spent,
            MAX(b.booking_date) as last_booking
        FROM users u
        JOIN bookings b ON u.user_id = b.user_id
        WHERE b.booking_date BETWEEN %s AND %s AND u.role = 'passenger'
        GROUP BY u.user_id, u.full_name, u.email
        ORDER BY total_bookings DESC
        LIMIT 10
    """, (start_date, end_date))

    top_users = cursor.fetchall()

    if top_users:
        df_users = pd.DataFrame(top_users, columns=['Name', 'Email', 'Bookings', 'Total Spent', 'Last Booking'])

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Top 10 Users by Bookings**")
            st.dataframe(df_users, use_container_width=True, hide_index=True)

        with col2:
            fig = px.bar(df_users, x='Bookings', y='Name', orientation='h',
                         color='Total Spent', color_continuous_scale='Viridis',
                         height=400)
            fig.update_layout(showlegend=False, yaxis={'categoryorder': 'total ascending'})
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No user engagement data available")


    # Day-wise Analysis
    st.markdown('<h3 class="section-title">📅 Day of Week Analysis</h3>', unsafe_allow_html=True)

    cursor.execute("""
        SELECT 
            TO_CHAR(booking_date, 'Day') as day_name,
            EXTRACT(DOW FROM booking_date) as day_num,
            COUNT(*) as bookings,
            SUM(total_fare) as revenue
        FROM bookings
        WHERE booking_date BETWEEN %s AND %s
        GROUP BY day_name, day_num
        ORDER BY day_num
    """, (start_date, end_date))

    day_data = cursor.fetchall()

    if day_data:
        df_days = pd.DataFrame(day_data, columns=['Day', 'DayNum', 'Bookings', 'Revenue'])
        df_days['Day'] = df_days['Day'].str.strip()

        fig = go.Figure()
        fig.add_trace(go.Bar(x=df_days['Day'], y=df_days['Bookings'], name='Bookings', marker_color='#667eea'))
        fig.update_layout(title='Bookings by Day of Week', height=350, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

        busiest_day = df_days.loc[df_days['Bookings'].idxmax()]
        slowest_day = df_days.loc[df_days['Bookings'].idxmin()]

        col1, col2 = st.columns(2)
        with col1:
            st.success(f"🔥 Busiest: **{busiest_day['Day']}** ({int(busiest_day['Bookings'])} bookings)")
        with col2:
            st.info(f"📉 Slowest: **{slowest_day['Day']}** ({int(slowest_day['Bookings'])} bookings)")
    else:
        st.info("No day-wise data available")


    cursor.close()


def manage_bookings():
    """Admin: View and manage all bookings"""
    render_header("Booking Management")


    conn = get_connection()
    cursor = conn.cursor()

    # Filters
    col1, col2, col3 = st.columns(3)
    with col1:
        status_filter = st.selectbox("Status", ["All", "Confirmed", "Cancelled"])
    with col2:
        payment_filter = st.selectbox("Payment", ["All", "Completed", "Pending"])
    with col3:
        date_filter = st.date_input("Date", value=datetime.now().date())



    # Build query
    query = """
        SELECT b.booking_id, u.full_name, r.route_name, r.source, r.destination,
               b.booking_date, s.departure_time, b.num_passengers, b.total_fare,
               b.payment_status, b.booking_status, b.created_at
        FROM bookings b
        JOIN users u ON b.user_id = u.user_id
        JOIN schedules s ON b.schedule_id = s.schedule_id
        JOIN routes r ON s.route_id = r.route_id
        WHERE b.booking_date = %s
    """
    params = [date_filter]

    if status_filter != "All":
        query += " AND b.booking_status = %s"
        params.append(status_filter.lower())

    if payment_filter != "All":
        query += " AND b.payment_status = %s"
        params.append(payment_filter.lower())

    query += " ORDER BY b.created_at DESC"

    cursor.execute(query, params)
    bookings = cursor.fetchall()

    if bookings:
        df = pd.DataFrame(bookings, columns=[
            'ID', 'Passenger', 'Route', 'From', 'To', 'Date', 'Time',
            'Passengers', 'Fare', 'Payment', 'Status', 'Booked At'
        ])

        st.dataframe(df, use_container_width=True, hide_index=True)

        # Summary
    
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Total Bookings", len(bookings))
        with col2:
            confirmed = sum(1 for b in bookings if b[10] == 'confirmed')
            st.metric("Confirmed", confirmed)
        with col3:
            total_revenue = sum(float(b[8]) for b in bookings if b[9] == 'completed')
            st.metric("Revenue", f"₹{total_revenue:.2f}")
    else:
        st.info("No bookings found for the selected filters")

    cursor.close()


def admin_reports():
    """Admin reports and analytics"""
    render_header("Reports & Analytics")

    conn = get_connection()
    cursor = conn.cursor()

    # Date range selector
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("From Date", value=datetime.now().date() - timedelta(days=30))
    with col2:
        end_date = st.date_input("To Date", value=datetime.now().date())

    # Revenue Report
    st.markdown('<h3 class="section-title">Revenue Report</h3>', unsafe_allow_html=True)

    cursor.execute("""
        SELECT booking_date, COUNT(*), SUM(total_fare)
        FROM bookings
        WHERE booking_date BETWEEN %s AND %s
        GROUP BY booking_date
        ORDER BY booking_date
    """, (start_date, end_date))

    revenue_data = cursor.fetchall()

    if revenue_data:
        df = pd.DataFrame(revenue_data, columns=['Date', 'Bookings', 'Revenue'])

        col1, col2 = st.columns(2)
        with col1:
            fig = px.line(df, x='Date', y='Bookings', title='Daily Bookings', markers=True)
            fig.update_layout(height=300)
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            fig = px.line(df, x='Date', y='Revenue', title='Daily Revenue', markers=True,
                          color_discrete_sequence=['#764ba2'])
            fig.update_layout(height=300)
            st.plotly_chart(fig, use_container_width=True)

        st.markdown("#### Summary Statistics")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Bookings", df['Bookings'].sum())
        with col2:
            st.metric("Total Revenue", f"₹{df['Revenue'].sum():.2f}")
        with col3:
            st.metric("Avg Bookings/Day", f"{df['Bookings'].mean():.1f}")
        with col4:
            st.metric("Avg Revenue/Day", f"₹{df['Revenue'].mean():.2f}")
    else:
        st.info("No data available for selected date range")


    # Popular Routes
    st.markdown('<h3 class="section-title">Popular Routes</h3>', unsafe_allow_html=True)

    cursor.execute("""
        SELECT r.route_name, r.source, r.destination, COUNT(b.booking_id) as bookings, SUM(b.total_fare) as revenue
        FROM bookings b
        JOIN schedules s ON b.schedule_id = s.schedule_id
        JOIN routes r ON s.route_id = r.route_id
        WHERE b.booking_date BETWEEN %s AND %s
        GROUP BY r.route_id, r.route_name, r.source, r.destination
        ORDER BY bookings DESC
        LIMIT 10
    """, (start_date, end_date))

    popular_routes = cursor.fetchall()

    if popular_routes:
        df = pd.DataFrame(popular_routes, columns=['Route', 'From', 'To', 'Bookings', 'Revenue'])

        fig = px.bar(df, x='Route', y='Bookings', title='Top 10 Routes by Bookings',
                     color='Revenue', color_continuous_scale='Viridis')
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)

        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No route data available")


    # Bus Utilization
    st.markdown('<h3 class="section-title">Bus Utilization</h3>', unsafe_allow_html=True)

    cursor.execute("""
        SELECT b.bus_number, COUNT(bk.booking_id) as trips, SUM(bk.num_passengers) as passengers
        FROM bookings bk
        JOIN schedules s ON bk.schedule_id = s.schedule_id
        JOIN buses b ON s.bus_id = b.bus_id
        WHERE bk.booking_date BETWEEN %s AND %s
        GROUP BY b.bus_id, b.bus_number
        ORDER BY trips DESC
    """, (start_date, end_date))

    bus_data = cursor.fetchall()

    if bus_data:
        df = pd.DataFrame(bus_data, columns=['Bus Number', 'Trips', 'Passengers'])
        st.dataframe(df, use_container_width=True, hide_index=True)

    else:
        st.info("No bus utilization data available")

    cursor.close()


def admin_support():
    """Admin support ticket management"""
    render_header("Support Tickets")


    conn = get_connection()
    cursor = conn.cursor()

    st.subheader("Manage Support Tickets")
    
    # Filter by status
    status_filter = st.selectbox("Filter by Status", ["Open", "Closed", "All"])
    
    query = """
        SELECT t.ticket_id, u.username, t.subject, t.message, t.admin_reply, t.status, t.created_at
        FROM support_tickets t
        JOIN users u ON t.user_id = u.user_id
    """
    params = []
    
    if status_filter == "Open":
        query += " WHERE t.status = 'open'"
    elif status_filter == "Closed":
        query += " WHERE t.status = 'closed'"
        
    query += " ORDER BY t.created_at DESC"
    
    cursor.execute(query)
    tickets = cursor.fetchall()

    if tickets:
        for ticket in tickets:
            ticket_id, username, subject, message, reply, status, created_at = ticket
            
            with st.expander(f"#{ticket_id}: {subject} ({status.upper()})", expanded=(status=='open')):
                st.markdown(f"**From:** {username} | **Date:** {created_at.strftime('%Y-%m-%d %H:%M')}")
                st.info(f"**Message:** {message}")
                
                if status == 'open':
                    with st.form(key=f"reply_form_{ticket_id}"):
                        reply_text = st.text_area("Reply", height=100)
                        if st.form_submit_button("Send Reply & Close Ticket"):
                            if reply_text:
                                try:
                                    cursor.execute("""
                                        UPDATE support_tickets 
                                        SET admin_reply=%s, status='closed' 
                                        WHERE ticket_id=%s
                                    """, (reply_text, ticket_id))
                                    conn.commit()
                                    st.success(f"Ticket #{ticket_id} closed!")
                                    time.sleep(1)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error: {e}")
                            else:
                                st.warning("Please enter a reply")
                else:
                    st.success(f"**Admin Reply:** {reply}")
                    
    else:
        st.info("No tickets found.")

    cursor.close()



def view_routes():
    """View all available routes"""
    render_header("Available Routes")


    conn = get_connection()
    cursor = conn.cursor()

    # Search functionality
    col1, col2 = st.columns([3, 1])
    with col1:
        search = st.text_input("🔍 Search routes", placeholder="Enter source or destination...",
                               label_visibility="collapsed")
    with col2:
        filter_status = st.selectbox("Status", ["All", "Active", "Inactive"], label_visibility="collapsed")



    # Build query based on filters
    query = "SELECT route_id, route_name, source, destination, distance_km, fare, status FROM routes WHERE 1=1"
    params = []

    if search:
        query += " AND (LOWER(source) LIKE %s OR LOWER(destination) LIKE %s OR LOWER(route_name) LIKE %s)"
        search_param = f"%{search.lower()}%"
        params.extend([search_param, search_param, search_param])

    if filter_status != "All":
        query += " AND status = %s"
        params.append(filter_status.lower())

    query += " ORDER BY route_id DESC"

    cursor.execute(query, params)
    routes = cursor.fetchall()

    if routes:
        for route in routes:
            route_id, route_name, source, dest, distance, fare, status = route

            status_color = "🟢" if status == "active" else "🔴"

            with st.container():
                col1, col2, col3 = st.columns([3, 2, 1])
                with col1:
                    st.markdown(f"**{route_name}**")
                    st.caption(f"{source} → {dest}")
                with col2:
                    st.metric("Distance", f"{distance} km")
                with col3:
                    st.metric("Fare", f"₹{fare}")
                st.markdown(f"Status: {status_color} {status.title()}")
                st.divider()
    else:
        st.info("No routes found matching your criteria")

    cursor.close()


def schedule_lookup():
    """Look up bus schedules"""
    render_header("Bus Schedules")


    conn = get_connection()
    cursor = conn.cursor()

    # Get routes for selection
    cursor.execute("SELECT route_id, route_name, source, destination FROM routes WHERE status='active'")
    routes = cursor.fetchall()

    if not routes:
        st.warning("No active routes available")
        cursor.close()
        return

    route_options = {f"{r[1]} ({r[2]} → {r[3]})": r[0] for r in routes}
    selected_route = st.selectbox("Select Route", ["All Routes"] + list(route_options.keys()))



    # Get schedules
    if selected_route == "All Routes":
        cursor.execute("""
            SELECT b.bus_number, r.route_name, r.source, r.destination,
                   s.departure_time, s.arrival_time, s.days_of_week
            FROM schedules s
            JOIN buses b ON s.bus_id = b.bus_id
            JOIN routes r ON s.route_id = r.route_id
            WHERE s.status='active'
            ORDER BY r.route_name, s.departure_time
        """)
    else:
        route_id = route_options[selected_route]
        cursor.execute("""
            SELECT b.bus_number, r.route_name, r.source, r.destination,
                   s.departure_time, s.arrival_time, s.days_of_week
            FROM schedules s
            JOIN buses b ON s.bus_id = b.bus_id
            JOIN routes r ON s.route_id = r.route_id
            WHERE s.route_id=%s AND s.status='active'
            ORDER BY s.departure_time
        """, (route_id,))

    schedules = cursor.fetchall()

    if schedules:
        for schedule in schedules:
            bus_num, route_name, source, dest, dep_time, arr_time, days = schedule

            with st.container():
                col1, col2, col3 = st.columns([2, 2, 2])
                with col1:
                    st.markdown(f"**🚌 {bus_num}**")
                    st.caption(f"{route_name}")
                with col2:
                    st.markdown(f"**Departure:** {dep_time}")
                    st.markdown(f"**Arrival:** {arr_time}")
                with col3:
                    st.markdown(f"**Operating Days:**")
                    st.caption(days.replace(",", ", "))
                st.divider()
    else:
        st.info("No schedules available for this route")

    cursor.close()


def fare_calculator():
    """Calculate fare for journey"""
    render_header("Fare Calculator")


    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT route_id, route_name, source, destination, fare FROM routes WHERE status='active'")
    routes = cursor.fetchall()

    if not routes:
        st.warning("No routes available")
        cursor.close()
        return

    st.markdown("### Calculate Your Fare")


    col1, col2 = st.columns(2)

    with col1:
        route_options = {f"{r[1]} ({r[2]} → {r[3]})": (r[0], r[4]) for r in routes}
        selected_route = st.selectbox("Select Route", list(route_options.keys()))
        route_id, base_fare = route_options[selected_route]

    with col2:
        num_passengers = st.number_input("Number of Passengers", min_value=1, max_value=50, value=1)

    total_fare = base_fare * num_passengers


    st.markdown('<div class="info-box">', unsafe_allow_html=True)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Base Fare", f"₹{base_fare}")
    with col2:
        st.metric("Passengers", num_passengers)
    with col3:
        st.metric("Total Fare", f"₹{total_fare}", delta=f"+₹{total_fare - base_fare}" if num_passengers > 1 else None)




    # Show pricing breakdown
    st.markdown("#### Fare Breakdown")
    breakdown_data = {
        "Description": ["Per Passenger", "Number of Passengers", "Total Amount"],
        "Amount": [f"₹{base_fare}", num_passengers, f"₹{total_fare}"]
    }
    st.table(pd.DataFrame(breakdown_data))


    cursor.close()


def admin_feedback():
    """Admin feedback management"""
    render_header("User Feedback")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # Calculate Average Rating
    cursor.execute("SELECT AVG(rating) FROM feedback")
    avg_rating = cursor.fetchone()[0]
    avg_rating = round(avg_rating, 1) if avg_rating else 0.0
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(f"""
            <div class="stat-card">
                <div class="stat-label">⭐ Average Rating</div>
                <div class="stat-value">{avg_rating}/5.0</div>
            </div>
        """, unsafe_allow_html=True)
    
    st.markdown("### Recent Feedback")
    
    cursor.execute("""
        SELECT f.rating, f.comments, u.username, f.created_at
        FROM feedback f
        JOIN users u ON f.user_id = u.user_id
        ORDER BY f.created_at DESC
        LIMIT 20
    """)
    feedbacks = cursor.fetchall()
    
    if feedbacks:
        for fb in feedbacks:
            rating, comments, username, created_at = fb
            with st.container():
                st.markdown(f"""
                <div class="content-card">
                    <div style="display:flex; justify-content:space-between;">
                        <strong>{username}</strong>
                        <small>{created_at.strftime('%Y-%m-%d')}</small>
                    </div>
                    <div style="color: gold; font-size: 1.2rem;">{'★' * rating}{'☆' * (5-rating)}</div>
                    <p>{comments}</p>
                </div>
                """, unsafe_allow_html=True)
    else:
        st.info("No feedback received yet.")
        
    cursor.close()


def feedback_page():
    """User feedback page"""
    render_header("Give Feedback")
    
    st.write("We value your feedback! Please rate your experience.")
    
    with st.form("feedback_form"):
        # Use new feedback component
        sentiment_mapping = ["one", "two", "three", "four", "five"]
        selected = st.feedback("stars")
        
        comments = st.text_area("Comments", placeholder="Tell us what you liked or what we can improve...")
        
        if st.form_submit_button("Submit Feedback", type="primary"):
            if selected is not None:
                rating = selected + 1 # Convert 0-4 index to 1-5 rating
            if comments:
                try:
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO feedback (user_id, rating, comments)
                        VALUES (%s, %s, %s)
                    """, (st.session_state.user['user_id'], rating, comments))
                    conn.commit()
                    cursor.close()
                    st.success("Thank you for your feedback! ❤️")
                    time.sleep(2)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error submitting feedback: {e}")
            else:
                st.warning("Please provide a rating and comments.")
    


def help_center():
    """Help center and FAQ"""
    render_header("Help Center")


    st.markdown("### Frequently Asked Questions")


    with st.expander("❓ How do I book a ticket?"):
        st.markdown("""
        1. Navigate to the **Book Ticket** page from the menu
        2. Select your desired route and journey date
        3. Choose a bus and departure time
        4. Enter the number of passengers
        5. Select your payment method
        6. Click **Confirm Booking & Pay** to complete your booking

        You'll receive a booking confirmation with your Booking ID and Transaction ID.
        """)

    with st.expander("❓ How can I cancel my booking?"):
        st.markdown("""
        1. Go to **My Bookings** page
        2. Find the booking you want to cancel
        3. Select the Booking ID from the dropdown
        4. Click the **Cancel** button

        Note: Only confirmed bookings can be cancelled.
        """)

    with st.expander("❓ How do I check bus schedules?"):
        st.markdown("""
        Visit the **Schedules** page to view all available bus timings:
        - View all routes or filter by specific route
        - Check departure and arrival times
        - See operating days for each bus
        """)

    with st.expander("❓ How is the fare calculated?"):
        st.markdown("""
        The fare is calculated based on:
        - **Base fare** of the selected route
        - **Number of passengers**

        Formula: Total Fare = Base Fare × Number of Passengers

        Use the **Fare Calculator** to estimate your journey cost.
        """)

    with st.expander("❓ Can I update my profile information?"):
        st.markdown("""
        Yes! Go to **My Profile** page where you can:
        - Update your name, email, and phone number
        - Change your password
        - View your booking statistics
        """)

    with st.expander("❓ What payment methods are accepted?"):
        st.markdown("""
        We accept multiple payment methods:
        - UPI
        - Credit Card
        - Debit Card
        - Net Banking
        """)


    st.markdown("### Contact Support")


    col1, col2 = st.columns(2)

    with col1:
        st.markdown("""
        **📞 Phone Support**  
        +91-1800-123-4567  
        (Mon-Sun: 6:00 AM - 10:00 PM)

        **📧 Email Support**  
        support@brts.com  
        (Response within 24 hours)
        """)

    with col2:
        st.markdown("""
        **🏢 Head Office**  
        BRTS Administrative Building  
        Transit Hub, Main Road  
        City - 400001

        **⏰ Office Hours**  
        Monday - Saturday: 9:00 AM - 6:00 PM
        """)



def user_profile():
    """User profile management"""
    render_header("My Profile")


    conn = get_connection()
    cursor = conn.cursor()

    # Get user details
    cursor.execute("""
        SELECT username, full_name, email, phone, created_at
        FROM users WHERE user_id=%s
    """, (st.session_state.user['user_id'],))

    user_data = cursor.fetchone()

    if user_data:
        username, full_name, email, phone, created_at = user_data

        # Display profile info
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("#### Account Information")
            st.markdown(f"**Username:** {username}")
            st.markdown(f"**Full Name:** {full_name}")
            st.markdown(f"**Email:** {email}")
            st.markdown(f"**Phone:** {phone}")
            st.markdown(f"**Member Since:** {created_at.strftime('%B %d, %Y')}")

        with col2:
            st.markdown("#### Booking Statistics")

            # Get booking stats
            cursor.execute("""
                SELECT COUNT(*), COALESCE(SUM(total_fare), 0)
                FROM bookings WHERE user_id=%s
            """, (st.session_state.user['user_id'],))
            total_bookings, total_spent = cursor.fetchone()

            cursor.execute("""
                SELECT COUNT(*) FROM bookings 
                WHERE user_id=%s AND booking_status='confirmed'
            """, (st.session_state.user['user_id'],))
            active_bookings = cursor.fetchone()[0]

            st.metric("Total Bookings", total_bookings)
            st.metric("Active Bookings", active_bookings)
            st.metric("Total Spent", f"₹{float(total_spent):.2f}")

    
        st.divider()

        # Update profile section
        st.markdown("#### Update Profile")

        with st.form("update_profile"):
            new_name = st.text_input("Full Name", value=full_name)
            new_email = st.text_input("Email", value=email)

            current_phone = phone.replace("+91", "") if phone else ""
            new_phone_number = st.text_input("🇮🇳 +91  Phone Number", value=current_phone, max_chars=10,
                                             label_visibility="visible", key="profile_phone")

            if st.form_submit_button("Update Profile", type="primary"):
                new_phone = f"+91{new_phone_number}" if new_phone_number else phone
                try:
                    cursor.execute("""
                        UPDATE users 
                        SET full_name=%s, email=%s, phone=%s
                        WHERE user_id=%s
                    """, (new_name, new_email, new_phone, st.session_state.user['user_id']))
                    conn.commit()
                    st.session_state.user['full_name'] = new_name
                    st.session_state.user['email'] = new_email
                    st.success("✅ Profile updated successfully!")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error updating profile: {e}")

    
        st.divider()


    
        st.divider()
        
        # Delete Account Section
        st.markdown("#### ⚠️ Danger Zone")
        st.markdown("Once you delete your account, there is no going back. Please be certain.")
        
        with st.expander("Delete Account", expanded=False):
            st.warning("This action cannot be undone. All your bookings and data will be permanently deleted.")
            delete_confirm = st.text_input("Type 'DELETE' to confirm account deletion")
            
            if st.button("Permanently Delete Account", type="primary", use_container_width=True):
                if delete_confirm == "DELETE":
                    try:
                        # Delete user data (cascade should handle rest if set up, but let's be safe)
                        # First delete payments, bookings, tickets
                        cursor.execute("DELETE FROM payments WHERE booking_id IN (SELECT booking_id FROM bookings WHERE user_id=%s)", (st.session_state.user['user_id'],))
                        cursor.execute("DELETE FROM bookings WHERE user_id=%s", (st.session_state.user['user_id'],))
                        cursor.execute("DELETE FROM support_tickets WHERE user_id=%s", (st.session_state.user['user_id'],))
                        cursor.execute("DELETE FROM users WHERE user_id=%s", (st.session_state.user['user_id'],))
                        conn.commit()
                        
                        st.success("Account deleted successfully.")
                        time.sleep(1)
                        st.session_state.logged_in = False
                        st.session_state.user = None
                        st.session_state.auth_page = 'login'
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error deleting account: {e}")
                else:
                    st.error("Please type 'DELETE' exactly to confirm.")

    cursor.close()


def my_bookings():
    """Clean bookings view"""
    render_header("My Bookings")


    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT b.booking_id, r.route_name, r.source, r.destination, 
               b.booking_date, s.departure_time, b.num_passengers, 
               b.total_fare, b.payment_status, b.booking_status
        FROM bookings b
        JOIN schedules s ON b.schedule_id = s.schedule_id
        JOIN routes r ON s.route_id = r.route_id
        WHERE b.user_id = %s
        ORDER BY b.booking_date DESC, s.departure_time DESC
    """, (st.session_state.user['user_id'],))

    bookings = cursor.fetchall()

    if bookings:
        df = pd.DataFrame(bookings, columns=[
            'ID', 'Route', 'From', 'To', 'Date', 'Time',
            'Passengers', 'Fare', 'Payment', 'Status'
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)

        booking_ids = [b[0] for b in bookings if b[9] == 'confirmed']

        if booking_ids:
        
            st.subheader("Cancel Booking")
            col1, col2, col3 = st.columns([2, 1, 1])
            with col1:
                cancel_booking_id = st.selectbox("Select Booking", booking_ids)
            with col2:
                confirm_cancel = st.checkbox("I confirm cancellation", key="confirm_cancel")
            with col3:
            
                if st.button("❌ Cancel Booking", type="secondary", use_container_width=True, disabled=not confirm_cancel):
                    try:
                        cursor.execute("""
                            UPDATE bookings SET booking_status='cancelled'
                            WHERE booking_id=%s AND user_id=%s
                        """, (cancel_booking_id, st.session_state.user['user_id']))
                        conn.commit()
                        st.toast(f"Booking #{cancel_booking_id} has been cancelled.", icon="❌")
                        st.success("Booking cancelled!")
                        time.sleep(1)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")
    else:
        st.info("You haven't made any bookings yet. Book your first ticket!")

    cursor.close()


# Main Application
def main():
    """Main application logic"""
    init_page()


    if 'db_initialized' not in st.session_state:
        if init_database():
            st.session_state.db_initialized = True

    if not st.session_state.logged_in:
        if st.session_state.auth_page == 'register':
            register_page()
        else:
            login_page()
        return

    # Sidebar
    with st.sidebar:
        st.markdown("### 🚌 BRTS Portal")
        st.markdown(f"**{st.session_state.user['full_name']}**")
        st.caption(f"{st.session_state.user['role'].title()}")

        # Time-based greeting
        current_hour = datetime.now().hour
        if current_hour < 12:
            greeting = "☀️ Good Morning"
        elif current_hour < 17:
            greeting = "🌤️ Good Afternoon"
        else:
            greeting = "🌙 Good Evening"
        st.markdown(f'<p class="sidebar-greeting">{greeting} • {datetime.now().strftime("%b %d, %I:%M %p")}</p>', unsafe_allow_html=True)

        st.divider()

        # Check if quick navigation was triggered
        if 'quick_nav' in st.session_state and st.session_state.quick_nav:
            default_page = st.session_state.quick_nav
            st.session_state.quick_nav = None
        else:
            default_page = None

        if st.session_state.user['role'] == 'admin':
            menu_options = ["📊 Dashboard", "📈 Analytics", "🚌 Buses", "🛣️ Routes", "⏰ Schedules", "📋 Bookings", "📑 Reports", "💬 Support", "⭐ Feedback"]
            # Map for page routing (strip emoji)
            menu_map = {opt: opt.split(" ", 1)[1] for opt in menu_options}
            if default_page:
                # Find the menu option that contains the default_page
                default_index = 0
                for i, opt in enumerate(menu_options):
                    if default_page in opt:
                        default_index = i
                        break
            else:
                default_index = 0

            selected = st.radio(
                "Menu",
                menu_options,
                index=default_index,
                label_visibility="collapsed"
            )
            page = menu_map[selected]
        else:
            menu_options = ["🎫 Book Ticket", "📋 My Bookings", "🛣️ View Routes", "⏰ Schedules", "💰 Fare Calculator", "👤 My Profile",
                            "⭐ Feedback", "❓ Help"]
            menu_map = {opt: opt.split(" ", 1)[1] for opt in menu_options}
            selected = st.radio(
                "Menu",
                menu_options,
                label_visibility="collapsed"
            )
            page = menu_map[selected]


        st.divider()

        # Moved theme toggle to top-right header
        # Kept divider for visual separation before logout

    
        if st.button("🚪 Logout", use_container_width=True):
            st.toast("Logged out successfully!", icon="👋")
            st.session_state.logged_in = False
            st.session_state.user = None
            st.session_state.auth_page = 'login'
            st.rerun()


    # Page routing
    if st.session_state.user['role'] == 'admin':
        if page == "Dashboard":
            admin_dashboard()
        elif page == "Analytics":
            analytics_dashboard()
        elif page == "Buses":
            manage_buses()
        elif page == "Routes":
            manage_routes()
        elif page == "Schedules":
            manage_schedules()
        elif page == "Bookings":
            manage_bookings()
        elif page == "Reports":
            admin_reports()
        elif page == "Support":
            admin_support()
        elif page == "Feedback":
            admin_feedback()
    else:
        if page == "Book Ticket":
            booking_system()
        elif page == "My Bookings":
            my_bookings()
        elif page == "View Routes":
            view_routes()
        elif page == "Schedules":
            schedule_lookup()
        elif page == "Fare Calculator":
            fare_calculator()
        elif page == "My Profile":
            user_profile()
        elif page == "Feedback":
            feedback_page()
        elif page == "Help":
            help_center()


def login_page():
    """Modern login page with lockout protection and admin access code verification."""
    st.markdown("""
        <div class="auth-logo">
            <h1 class="auth-logo-text">City Bus Portal</h1>
            <p class="auth-subtitle">Multi-City Urban Bus Transport</p>
        </div>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:
        st.markdown('<h2 class="auth-title">Sign in</h2>', unsafe_allow_html=True)

        login_locked = False
        if st.session_state.login_lock_until and datetime.now() < st.session_state.login_lock_until:
            remaining_seconds = max(1, int((st.session_state.login_lock_until - datetime.now()).total_seconds()))
            st.error(f"Too many failed login attempts. Try again in {remaining_seconds} seconds.")
            login_locked = True
        elif st.session_state.login_lock_until:
            st.session_state.login_lock_until = None
            st.session_state.login_attempts = 0

        pending_admin_user = st.session_state.get("pending_admin_user")
        if pending_admin_user:
            st.info("Additional verification required.")
            with st.form("admin_verification_form", clear_on_submit=False):
                admin_access_code = st.text_input(
                    "Admin Access Code",
                    type="password",
                    placeholder="Enter admin access code"
                )
                verify_submitted = st.form_submit_button(
                    "Verify & Sign in",
                    use_container_width=True,
                    type="primary",
                    disabled=login_locked
                )
                if verify_submitted:
                    if not ADMIN_ACCESS_CODE:
                        st.error("Admin access code is not configured. Set CITYBUS_ADMIN_ACCESS_CODE in environment.")
                    elif not admin_access_code.strip():
                        st.warning("Please enter admin access code.")
                    elif not hmac.compare_digest(admin_access_code.strip(), ADMIN_ACCESS_CODE):
                        st.session_state.login_attempts += 1
                        attempts_left = MAX_LOGIN_ATTEMPTS - st.session_state.login_attempts
                        if attempts_left <= 0:
                            st.session_state.login_lock_until = datetime.now() + timedelta(minutes=LOGIN_LOCK_MINUTES)
                            st.session_state.pending_admin_user = None
                            st.error(f"Too many failed attempts. Login locked for {LOGIN_LOCK_MINUTES} minutes.")
                        else:
                            st.error(f"Invalid admin access code. Attempts left: {attempts_left}")
                    else:
                        conn = get_connection()
                        if not conn:
                            st.error("Database is unavailable. Please try again.")
                        else:
                            cursor = conn.cursor()
                            cursor.execute(
                                "SELECT role FROM users WHERE user_id=%s AND username=%s",
                                (pending_admin_user.get("user_id"), pending_admin_user.get("username"))
                            )
                            db_user = cursor.fetchone()
                            cursor.close()
                            if not db_user or db_user[0] != "admin":
                                st.session_state.pending_admin_user = None
                                st.error("Admin privileges could not be verified.")
                            else:
                                st.session_state.logged_in = True
                                st.session_state.user = pending_admin_user
                                st.session_state.admin_verified = True
                                st.session_state.pending_admin_user = None
                                st.session_state.login_attempts = 0
                                st.session_state.login_lock_until = None
                                st.success(f"Welcome back, {pending_admin_user['full_name']}!")
                                time.sleep(0.4)
                                st.rerun()

            if st.button("Back to normal sign in", use_container_width=True, key="admin_back_to_login"):
                st.session_state.pending_admin_user = None
                st.rerun()
        else:
            with st.form("login_form", clear_on_submit=False):
                login_id = st.text_input("Username or email", placeholder="Enter your username or email")
                password = st.text_input("Password", type="password", placeholder="Enter your password")

                submitted = st.form_submit_button(
                    "Sign in",
                    use_container_width=True,
                    type="primary",
                    disabled=login_locked
                )

                if submitted:
                    if not login_id or not password:
                        st.warning("Please enter both username/email and password.")
                    else:
                        user, auth_error = verify_user(login_id.strip(), password, require_admin_code=False)
                        if user:
                            if user["role"] == "admin":
                                st.session_state.pending_admin_user = user
                                st.info("Admin account detected. Continue with secure verification.")
                                st.rerun()
                            else:
                                st.session_state.logged_in = True
                                st.session_state.user = user
                                st.session_state.admin_verified = False
                                st.session_state.login_attempts = 0
                                st.session_state.login_lock_until = None
                                st.success(f"Welcome back, {user['full_name']}!")
                                time.sleep(0.4)
                                st.rerun()
                        else:
                            st.session_state.login_attempts += 1
                            attempts_left = MAX_LOGIN_ATTEMPTS - st.session_state.login_attempts
                            if attempts_left <= 0:
                                st.session_state.login_lock_until = datetime.now() + timedelta(minutes=LOGIN_LOCK_MINUTES)
                                st.error(f"Too many failed attempts. Login locked for {LOGIN_LOCK_MINUTES} minutes.")
                            else:
                                st.error(f"{auth_error} Attempts left: {attempts_left}")

        if st.button("Create account", use_container_width=True, key="goto_register"):
            st.session_state.auth_page = 'register'
            st.rerun()

        if st.session_state.get("pending_admin_user"):
            st.caption("Administrator identity is being verified.")


def register_page():
    """Passenger registration with preferred city selection."""
    st.markdown("""
        <div class="auth-logo">
            <h1 class="auth-logo-text">City Bus Portal</h1>
            <p class="auth-subtitle">Multi-City Urban Bus Transport</p>
        </div>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:
        st.markdown('<h2 class="auth-title">Create your account</h2>', unsafe_allow_html=True)

        with st.form("register_form_v2", clear_on_submit=False):
            full_name = st.text_input("Full Name", placeholder="John Doe")
            email = st.text_input("Email", placeholder="john@example.com")
            username = st.text_input("Username", placeholder="Choose a username")
            preferred_city = st.selectbox("Preferred City", SUPPORTED_CITIES, index=city_index(DEFAULT_CITY))
            phone_number = st.text_input("Phone Number (+91)", placeholder="9876543210", max_chars=10)
            password = st.text_input("Password", type="password", placeholder="Create a strong password")
            confirm_password = st.text_input("Confirm Password", type="password", placeholder="Re-enter your password")

            submitted = st.form_submit_button("Create account", use_container_width=True, type="primary")

            if submitted:
                phone = f"+91{phone_number}" if phone_number else ""
                if not all([full_name, email, username, password, confirm_password, phone_number]):
                    st.warning("Please fill in all required fields.")
                elif len(phone_number) != 10 or not phone_number.isdigit():
                    st.error("Phone number must be exactly 10 digits.")
                elif password != confirm_password:
                    st.error("Passwords do not match.")
                elif len(password) < 6:
                    st.error("Password must be at least 6 characters.")
                else:
                    conn = get_connection()
                    if not conn:
                        st.error("Database is unavailable. Please try again.")
                    else:
                        try:
                            cursor = conn.cursor()
                            cursor.execute("""
                                INSERT INTO users (username, password, role, full_name, email, phone, preferred_city)
                                VALUES (%s, %s, 'passenger', %s, %s, %s, %s)
                            """, (username.strip(), hash_password(password), full_name, email.strip(), phone, preferred_city))
                            conn.commit()
                            cursor.close()
                            st.success("Account created successfully.")
                            st.info("Redirecting to login...")
                            time.sleep(1)
                            st.session_state.auth_page = 'login'
                            st.rerun()
                        except Exception as e:
                            if 'duplicate key' in str(e).lower():
                                st.error("Username already exists.")
                            else:
                                st.error(f"Registration failed: {e}")

        if st.button("Back to Sign in", use_container_width=True, key="goto_login"):
            st.session_state.auth_page = 'login'
            st.rerun()


def manage_buses():
    """City-wise bus management."""
    render_header("Bus Management")
    selected_city = st.selectbox("City", SUPPORTED_CITIES, key="bus_mgmt_city")
    tab1, tab2, tab3, tab4 = st.tabs(["View All", "Add Bus", "Edit Bus", "Delete Bus"])

    conn = get_connection()
    if not conn:
        return

    with tab1:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT bus_id, bus_number, city, capacity, bus_type, status
            FROM buses
            WHERE city=%s
            ORDER BY bus_id DESC
        """, (selected_city,))
        buses = cursor.fetchall()
        if buses:
            df = pd.DataFrame(buses, columns=['ID', 'Bus Number', 'City', 'Capacity', 'Type', 'Status'])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info(f"No buses found for {selected_city}.")
        cursor.close()

    with tab2:
        col1, col2 = st.columns(2)
        with col1:
            bus_number = st.text_input("Bus Number", placeholder="e.g. CB-101")
            bus_type = st.selectbox("Bus Type", ["Standard", "AC", "Luxury", "Electric"], key="add_bus_type_v2")
        with col2:
            capacity = st.number_input("Capacity", min_value=10, max_value=100, value=40, key="add_bus_cap_v2")
            status = st.selectbox("Status", ["active", "inactive", "maintenance"], key="add_bus_status_v2")

        if st.button("Add Bus", type="primary", use_container_width=True, key="add_bus_btn_v2"):
            if not bus_number.strip():
                st.warning("Please enter bus number.")
            else:
                try:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO buses (bus_number, city, capacity, bus_type, status)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (bus_number.strip(), selected_city, capacity, bus_type, status))
                    conn.commit()
                    cursor.close()
                    st.success(f"Bus {bus_number.strip()} added for {selected_city}.")
                    time.sleep(0.6)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

    with tab3:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT bus_id, bus_number, city, capacity, bus_type, status
            FROM buses
            WHERE city=%s
            ORDER BY bus_number
        """, (selected_city,))
        all_buses = cursor.fetchall()

        if not all_buses:
            st.info(f"No buses to edit in {selected_city}.")
        else:
            bus_edit_options = {f"{b[1]} (ID: {b[0]})": b for b in all_buses}
            selected_edit = st.selectbox("Select Bus to Edit", list(bus_edit_options.keys()), key="edit_bus_sel_v2")
            bus_data = bus_edit_options[selected_edit]

            with st.form("edit_bus_form_v2"):
                col1, col2 = st.columns(2)
                with col1:
                    edit_bus_number = st.text_input("Bus Number", value=bus_data[1])
                    type_options = ["Standard", "AC", "Luxury", "Electric"]
                    current_type_idx = type_options.index(bus_data[4]) if bus_data[4] in type_options else 0
                    edit_bus_type = st.selectbox("Bus Type", type_options, index=current_type_idx)
                with col2:
                    edit_capacity = st.number_input("Capacity", min_value=10, max_value=100, value=int(bus_data[3]))
                    status_options = ["active", "inactive", "maintenance"]
                    current_status_idx = status_options.index(bus_data[5]) if bus_data[5] in status_options else 0
                    edit_status = st.selectbox("Status", status_options, index=current_status_idx)

                if st.form_submit_button("Update Bus", type="primary", use_container_width=True):
                    try:
                        cursor.execute("""
                            UPDATE buses
                            SET bus_number=%s, capacity=%s, bus_type=%s, status=%s
                            WHERE bus_id=%s AND city=%s
                        """, (edit_bus_number.strip(), edit_capacity, edit_bus_type, edit_status, bus_data[0], selected_city))
                        conn.commit()
                        st.success(f"Bus '{edit_bus_number.strip()}' updated.")
                        time.sleep(0.5)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")
        cursor.close()

    with tab4:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT bus_id, bus_number, status
            FROM buses
            WHERE city=%s
            ORDER BY bus_number
        """, (selected_city,))
        del_buses = cursor.fetchall()

        if not del_buses:
            st.info(f"No buses to delete in {selected_city}.")
        else:
            bus_del_options = {f"{b[1]} (ID: {b[0]}) - {b[2]}": b[0] for b in del_buses}
            selected_del = st.selectbox("Select Bus to Delete", list(bus_del_options.keys()), key="del_bus_sel_v2")
            bus_del_id = bus_del_options[selected_del]
            confirm_del = st.checkbox("I confirm I want to delete this bus", key="confirm_del_bus_v2")

            if st.button("Delete Bus", type="primary", use_container_width=True, disabled=not confirm_del, key="del_bus_btn_v2"):
                try:
                    cursor.execute("DELETE FROM schedules WHERE bus_id=%s", (bus_del_id,))
                    cursor.execute("DELETE FROM buses WHERE bus_id=%s AND city=%s", (bus_del_id, selected_city))
                    conn.commit()
                    st.success("Bus deleted successfully.")
                    time.sleep(0.5)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
        cursor.close()


def manage_routes():
    """City-wise route management with stops."""
    render_header("Route Management")
    selected_city = st.selectbox("City", SUPPORTED_CITIES, key="route_mgmt_city")
    tab1, tab2, tab3, tab4 = st.tabs(["Routes", "Add Route", "Edit Route", "Delete Route"])

    conn = get_connection()
    if not conn:
        return
    cursor = conn.cursor()

    with tab1:
        cursor.execute("""
            SELECT route_id, route_name, city, source, destination, distance_km, fare, status
            FROM routes
            WHERE city=%s
            ORDER BY route_id
        """, (selected_city,))
        routes = cursor.fetchall()

        if routes:
            df = pd.DataFrame(routes, columns=['ID', 'Name', 'City', 'Source', 'Destination', 'Distance (km)', 'Fare', 'Status'])
            st.dataframe(df, use_container_width=True, hide_index=True)

            st.divider()
            route_stop_options = {f"{r[1]}": r[0] for r in routes}
            view_route_name = st.selectbox("Select Route for Stops", list(route_stop_options.keys()), key="view_stops_route_v2")
            view_route_id = route_stop_options[view_route_name]
            cursor.execute("""
                SELECT stop_id, stop_name, stop_order, arrival_time
                FROM stops
                WHERE route_id=%s
                ORDER BY stop_order
            """, (view_route_id,))
            route_stops = cursor.fetchall()
            if route_stops:
                stops_df = pd.DataFrame(route_stops, columns=['ID', 'Stop Name', 'Order', 'Arrival Time'])
                st.dataframe(stops_df, use_container_width=True, hide_index=True)
            else:
                st.info("No stops defined for this route.")
        else:
            st.info(f"No routes found for {selected_city}.")

    with tab2:
        col1, col2 = st.columns(2)
        with col1:
            route_name = st.text_input("Route Name", placeholder="e.g. Route 101")
            source = st.text_input("Source", placeholder="Starting Point")
            destination = st.text_input("Destination", placeholder="End Point")
        with col2:
            distance = st.number_input("Distance (km)", min_value=0.1, step=0.1)
            fare = st.number_input("Fare (INR)", min_value=1.0, step=0.5)

        if st.button("Add Route", type="primary", use_container_width=True, key="add_route_btn_v2"):
            if route_name and source and destination:
                try:
                    cursor.execute("""
                        INSERT INTO routes (route_name, source, destination, distance_km, fare, city)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING route_id
                    """, (route_name, source, destination, distance, fare, selected_city))
                    new_route_id = cursor.fetchone()[0]
                    cursor.execute("""
                        INSERT INTO stops (route_id, stop_name, stop_order, arrival_time)
                        VALUES (%s, %s, 1, '00:00'), (%s, %s, 2, '00:00')
                    """, (new_route_id, source, new_route_id, destination))
                    conn.commit()
                    st.success(f"Route '{route_name}' added for {selected_city}.")
                    time.sleep(0.6)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
            else:
                st.warning("Please fill all required fields.")

        st.divider()
        st.subheader("Add Stop to Route")
        cursor.execute("""
            SELECT route_id, route_name
            FROM routes
            WHERE city=%s AND status='active'
            ORDER BY route_name
        """, (selected_city,))
        active_routes_add = cursor.fetchall()

        if active_routes_add:
            add_stop_route_opts = {f"{r[1]}": r[0] for r in active_routes_add}
            sel_route_for_stop = st.selectbox(
                "Select Route",
                list(add_stop_route_opts.keys()),
                key="add_stop_route_v2"
            )
            sel_route_id = add_stop_route_opts[sel_route_for_stop]

            cursor.execute(
                "SELECT stop_order FROM stops WHERE route_id=%s ORDER BY stop_order DESC LIMIT 1",
                (sel_route_id,)
            )
            last_order = cursor.fetchone()
            next_order = (last_order[0] + 1) if last_order else 1

            col_a, col_b, col_c = st.columns(3)
            with col_a:
                stop_name = st.text_input(
                    "Stop Name",
                    placeholder="e.g. Central Station",
                    key="new_stop_name_v2"
                )
            with col_b:
                stop_order = st.number_input(
                    "Stop Order",
                    min_value=1,
                    value=next_order,
                    step=1,
                    key="new_stop_order_v2"
                )
            with col_c:
                default_time = datetime.strptime('09:00', '%H:%M').time()
                arrival_time_input = st.time_input(
                    "Approx. Arrival Time",
                    value=default_time,
                    key="new_stop_time_v2"
                )

            if st.button("Add Stop", type="primary", use_container_width=True, key="add_stop_btn_v2"):
                if stop_name:
                    try:
                        cursor.execute("""
                            INSERT INTO stops (route_id, stop_name, stop_order, arrival_time)
                            VALUES (%s, %s, %s, %s)
                        """, (sel_route_id, stop_name, stop_order, arrival_time_input))
                        conn.commit()
                        st.success(f"Stop '{stop_name}' added to route.")
                        time.sleep(0.4)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error adding stop: {e}")
                else:
                    st.warning("Please enter a stop name.")
        else:
            st.info(f"No active routes in {selected_city}. Add or activate a route first.")

    with tab3:
        cursor.execute("""
            SELECT route_id, route_name, source, destination, distance_km, fare, status
            FROM routes
            WHERE city=%s
            ORDER BY route_name
        """, (selected_city,))
        edit_routes = cursor.fetchall()
        if not edit_routes:
            st.info(f"No routes to edit in {selected_city}.")
        else:
            edit_route_opts = {f"{r[1]} (ID: {r[0]})": r for r in edit_routes}
            sel_edit_route = st.selectbox("Select Route to Edit", list(edit_route_opts.keys()), key="edit_route_sel_v2")
            route_data = edit_route_opts[sel_edit_route]

            with st.form("edit_route_form_v2"):
                col1, col2 = st.columns(2)
                with col1:
                    edit_route_name = st.text_input("Route Name", value=route_data[1])
                    edit_source = st.text_input("Source", value=route_data[2])
                    edit_destination = st.text_input("Destination", value=route_data[3])
                with col2:
                    edit_distance = st.number_input("Distance (km)", min_value=0.1, step=0.1, value=float(route_data[4]))
                    edit_fare = st.number_input("Fare (INR)", min_value=1.0, step=0.5, value=float(route_data[5]))
                    status_opts = ["active", "inactive"]
                    current_idx = status_opts.index(route_data[6]) if route_data[6] in status_opts else 0
                    edit_route_status = st.selectbox("Status", status_opts, index=current_idx)

                if st.form_submit_button("Update Route", type="primary", use_container_width=True):
                    try:
                        cursor.execute("""
                            UPDATE routes
                            SET route_name=%s, source=%s, destination=%s, distance_km=%s, fare=%s, status=%s
                            WHERE route_id=%s AND city=%s
                        """, (edit_route_name, edit_source, edit_destination, edit_distance, edit_fare, edit_route_status, route_data[0], selected_city))
                        conn.commit()
                        st.success("Route updated.")
                        time.sleep(0.5)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

            st.divider()
            st.subheader(f"Edit Stops for {route_data[1]}")
            cursor.execute("""
                SELECT stop_id, stop_name, stop_order, arrival_time
                FROM stops
                WHERE route_id=%s
                ORDER BY stop_order
            """, (route_data[0],))
            edit_stops = cursor.fetchall()

            if edit_stops:
                for s in edit_stops:
                    with st.expander(f"Stop #{s[2]}: {s[1]}"):
                        with st.form(f"edit_stop_v2_{s[0]}"):
                            e_col1, e_col2, e_col3 = st.columns(3)
                            with e_col1:
                                e_stop_name = st.text_input("Name", value=s[1], key=f"esn_v2_{s[0]}")
                            with e_col2:
                                e_stop_order = st.number_input("Order", min_value=1, value=s[2], key=f"eso_v2_{s[0]}")
                            with e_col3:
                                try:
                                    e_arrival = s[3] if s[3] else datetime.strptime('09:00', '%H:%M').time()
                                except Exception:
                                    e_arrival = datetime.strptime('09:00', '%H:%M').time()
                                e_arrival_time = st.time_input("Arrival", value=e_arrival, key=f"eat_v2_{s[0]}")

                            if st.form_submit_button("Save Stop", use_container_width=True):
                                try:
                                    cursor.execute("""
                                        UPDATE stops
                                        SET stop_name=%s, stop_order=%s, arrival_time=%s
                                        WHERE stop_id=%s
                                    """, (e_stop_name, e_stop_order, e_arrival_time, s[0]))
                                    conn.commit()
                                    st.success("Stop updated.")
                                    time.sleep(0.3)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error updating stop: {e}")

                st.divider()
                st.subheader("Remove Stop")
                del_stop_opts = {f"#{s[2]}: {s[1]} (ID: {s[0]})": s[0] for s in edit_stops}
                sel_del_stop = st.selectbox("Select Stop to Remove", list(del_stop_opts.keys()), key="del_stop_sel_v2")
                del_stop_id = del_stop_opts[sel_del_stop]

                if st.button("Remove Stop", type="primary", use_container_width=True, key="del_stop_btn_v2"):
                    try:
                        cursor.execute("DELETE FROM stops WHERE stop_id=%s", (del_stop_id,))
                        conn.commit()
                        st.success("Stop removed.")
                        time.sleep(0.3)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error removing stop: {e}")
            else:
                st.info("No stops defined for this route yet.")

    with tab4:
        cursor.execute("""
            SELECT route_id, route_name, source, destination, status
            FROM routes
            WHERE city=%s
            ORDER BY route_name
        """, (selected_city,))
        del_routes = cursor.fetchall()
        if not del_routes:
            st.info(f"No routes to delete in {selected_city}.")
        else:
            del_route_opts = {f"{r[1]} ({r[2]} -> {r[3]}) - {r[4]}": r[0] for r in del_routes}
            sel_del_route = st.selectbox("Select Route to Delete", list(del_route_opts.keys()), key="del_route_sel_v2")
            del_route_id = del_route_opts[sel_del_route]
            confirm_del_route = st.checkbox("I confirm I want to delete this route", key="confirm_del_route_v2")

            if st.button("Delete Route", type="primary", use_container_width=True, disabled=not confirm_del_route, key="del_route_btn_v2"):
                try:
                    cursor.execute("DELETE FROM stops WHERE route_id=%s", (del_route_id,))
                    cursor.execute("DELETE FROM schedules WHERE route_id=%s", (del_route_id,))
                    cursor.execute("DELETE FROM routes WHERE route_id=%s AND city=%s", (del_route_id, selected_city))
                    conn.commit()
                    st.success("Route and linked data deleted.")
                    time.sleep(0.5)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

    cursor.close()


def manage_schedules():
    """City-wise schedule management."""
    render_header("Schedule Management")
    selected_city = st.selectbox("City", SUPPORTED_CITIES, key="schedule_mgmt_city")
    tab1, tab2 = st.tabs(["View Schedules", "Add Schedule"])

    conn = get_connection()
    if not conn:
        return

    with tab1:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.schedule_id, r.city, b.bus_number, r.route_name, s.departure_time, s.arrival_time, s.days_of_week, s.status
            FROM schedules s
            JOIN buses b ON s.bus_id = b.bus_id
            JOIN routes r ON s.route_id = r.route_id
            WHERE r.city=%s
            ORDER BY s.schedule_id DESC
        """, (selected_city,))
        schedules = cursor.fetchall()
        if schedules:
            df = pd.DataFrame(schedules, columns=['ID', 'City', 'Bus', 'Route', 'Departure', 'Arrival', 'Days', 'Status'])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info(f"No schedules found for {selected_city}.")
        cursor.close()

    with tab2:
        cursor = conn.cursor()
        cursor.execute("SELECT bus_id, bus_number FROM buses WHERE status='active' AND city=%s ORDER BY bus_number", (selected_city,))
        buses = cursor.fetchall()
        cursor.execute("SELECT route_id, route_name FROM routes WHERE status='active' AND city=%s ORDER BY route_name", (selected_city,))
        routes = cursor.fetchall()

        if not buses or not routes:
            st.warning(f"Please add active buses and routes for {selected_city} first.")
            cursor.close()
            return

        col1, col2 = st.columns(2)
        with col1:
            bus_options = {f"{b[1]}": b[0] for b in buses}
            selected_bus = st.selectbox("Select Bus", list(bus_options.keys()), key="schedule_bus_v2")
            bus_id = bus_options[selected_bus]
            departure_time = st.time_input("Departure Time", key="schedule_departure_v2")
        with col2:
            route_options = {f"{r[1]}": r[0] for r in routes}
            selected_route = st.selectbox("Select Route", list(route_options.keys()), key="schedule_route_v2")
            route_id = route_options[selected_route]
            arrival_time = st.time_input("Arrival Time", key="schedule_arrival_v2")

        days_of_week = st.multiselect(
            "Operating Days",
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
            default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
            key="schedule_days_v2"
        )

        if st.button("Add Schedule", type="primary", use_container_width=True, key="add_schedule_btn_v2"):
            if not days_of_week:
                st.warning("Please select at least one day.")
            else:
                try:
                    days_str = ",".join(days_of_week)
                    cursor.execute("""
                        INSERT INTO schedules (bus_id, route_id, departure_time, arrival_time, days_of_week)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (bus_id, route_id, departure_time, arrival_time, days_str))
                    conn.commit()
                    st.success(f"Schedule added for {selected_city}.")
                    time.sleep(0.5)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
        cursor.close()


def booking_system():
    """City-first segment booking."""
    render_header("Book Your Ticket")
    conn = get_connection()
    if not conn:
        return
    cursor = conn.cursor()

    default_city = preferred_city_for_user(st.session_state.user)
    city = st.selectbox("Select City", SUPPORTED_CITIES, index=city_index(default_city), key="booking_city_v2")

    cursor.execute("""
        SELECT route_id, route_name, source, destination, fare
        FROM routes
        WHERE status='active' AND city=%s
        ORDER BY route_name
    """, (city,))
    routes = cursor.fetchall()
    if not routes:
        st.warning(f"No routes available currently in {city}.")
        cursor.close()
        return

    route_options = {f"{r[1]} ({r[2]} -> {r[3]})": r for r in routes}
    selected_route_key = st.selectbox("Select Route", list(route_options.keys()), key="booking_route_v2")
    route_data = route_options[selected_route_key]
    route_id, _, _, _, route_fare = route_data

    cursor.execute("SELECT stop_id, stop_name, stop_order FROM stops WHERE route_id=%s ORDER BY stop_order", (route_id,))
    stops = cursor.fetchall()
    if not stops or len(stops) < 2:
        st.warning("This route does not have enough stops defined yet.")
        cursor.close()
        return

    stop_options = {s[1]: (s[0], s[2]) for s in stops}
    stop_names = [s[1] for s in stops]

    col1, col2 = st.columns(2)
    with col1:
        source_stop_name = st.selectbox("From Stop", stop_names, key="source_stop_v2")
        source_id, source_order = stop_options[source_stop_name]
    with col2:
        dest_options = [s[1] for s in stops if s[2] > source_order]
        if not dest_options:
            st.error("Select a valid source stop.")
            cursor.close()
            return
        dest_stop_name = st.selectbox("To Stop", dest_options, key="dest_stop_v2")
        dest_id, dest_order = stop_options[dest_stop_name]

    col_d, col_p = st.columns(2)
    with col_d:
        booking_date = st.date_input("Journey Date", min_value=datetime.now().date(), key="booking_date_v2")
    with col_p:
        num_passengers = st.number_input("Number of Passengers", min_value=1, max_value=5, value=1, key="booking_pax_v2")

    cursor.execute("""
        SELECT s.schedule_id, b.bus_number, s.departure_time, s.arrival_time, b.bus_type, s.days_of_week
        FROM schedules s
        JOIN buses b ON s.bus_id = b.bus_id
        WHERE s.route_id=%s AND s.status='active' AND b.status='active' AND b.city=%s
    """, (route_id, city))
    schedules = cursor.fetchall()
    if not schedules:
        st.warning("No buses scheduled for this route.")
        cursor.close()
        return

    day_name = booking_date.strftime("%A")
    valid_schedules = []
    for sch in schedules:
        running_days = [d.strip() for d in (sch[5] or "").split(",") if d.strip()]
        if running_days and day_name not in running_days:
            continue
        is_avail, _, _ = check_segment_availability(sch[0], booking_date, source_order, dest_order, num_passengers)
        if is_avail:
            valid_schedules.append(sch)

    if not valid_schedules:
        st.error("No buses are running with enough seats for this date and route segment.")
        cursor.close()
        return

    schedule_options = {f"{s[1]} ({s[4]}) - Departs {s[2]}": s[0] for s in valid_schedules}
    selected_schedule_key = st.selectbox("Select Bus", list(schedule_options.keys()), key="booking_schedule_v2")
    schedule_id = schedule_options[selected_schedule_key]

    total_stops_count = len(stops)
    stops_travelled = dest_order - source_order
    if total_stops_count > 1:
        calculated_fare = (float(route_fare) / (total_stops_count - 1)) * stops_travelled
    else:
        calculated_fare = float(route_fare)
    calculated_fare = max(5.0, round(calculated_fare))
    total_amount = calculated_fare * num_passengers

    c1, c2, c3 = st.columns(3)
    c1.metric("Fare per Seat", f"INR {calculated_fare}")
    c2.metric("Passengers", num_passengers)
    c3.metric("Total Payable", f"INR {total_amount}")

    payment_method = st.selectbox("Payment Method", ["UPI", "Credit Card", "Debit Card", "Net Banking"], key="payment_method_v2")

    if not check_user_ticket_limit(st.session_state.user['user_id']):
        st.error("You have reached the maximum limit of 5 active bookings.")
    else:
        if st.button("Confirm Booking & Pay", type="primary", use_container_width=True, key="confirm_booking_btn_v2"):
            try:
                is_avail, _, _ = check_segment_availability(schedule_id, booking_date, source_order, dest_order, num_passengers)
                if not is_avail:
                    st.error("Sorry, seats were just booked by someone else.")
                else:
                    cursor.execute("""
                        INSERT INTO bookings (user_id, schedule_id, booking_date, num_passengers, total_fare,
                                              payment_status, source_stop_id, dest_stop_id)
                        VALUES (%s, %s, %s, %s, %s, 'completed', %s, %s)
                        RETURNING booking_id
                    """, (st.session_state.user['user_id'], schedule_id, booking_date, num_passengers, total_amount, source_id, dest_id))
                    booking_id = cursor.fetchone()[0]
                    transaction_id = f"TXN{booking_id}{int(time.time())}"
                    cursor.execute("""
                        INSERT INTO payments (booking_id, amount, payment_method, transaction_id)
                        VALUES (%s, %s, %s, %s)
                    """, (booking_id, total_amount, payment_method, transaction_id))
                    conn.commit()
                    st.success(f"Booking confirmed. Booking ID: {booking_id}")
            except Exception as e:
                st.error(f"Booking failed: {e}")

    cursor.close()


def view_routes():
    """City-wise route browser."""
    render_header("Available Routes")
    conn = get_connection()
    if not conn:
        return
    cursor = conn.cursor()

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        search = st.text_input("Search routes", placeholder="Enter source, destination, or route name")
    with col2:
        city_filter = st.selectbox("City", SUPPORTED_CITIES, index=city_index(preferred_city_for_user(st.session_state.user)), key="routes_city_filter_v2")
    with col3:
        status_filter = st.selectbox("Status", ["All", "Active", "Inactive"], key="routes_status_filter_v2")

    query = """
        SELECT route_id, route_name, city, source, destination, distance_km, fare, status
        FROM routes
        WHERE city=%s
    """
    params = [city_filter]

    if search:
        query += " AND (LOWER(source) LIKE %s OR LOWER(destination) LIKE %s OR LOWER(route_name) LIKE %s)"
        search_param = f"%{search.lower()}%"
        params.extend([search_param, search_param, search_param])

    if status_filter != "All":
        query += " AND status=%s"
        params.append(status_filter.lower())

    query += " ORDER BY route_id DESC"

    cursor.execute(query, params)
    routes = cursor.fetchall()
    if routes:
        df = pd.DataFrame(routes, columns=['ID', 'Route', 'City', 'Source', 'Destination', 'Distance (km)', 'Fare', 'Status'])
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No routes found matching your criteria.")

    cursor.close()


def schedule_lookup():
    """City-wise schedule lookup for users."""
    render_header("Bus Schedules")
    conn = get_connection()
    if not conn:
        return
    cursor = conn.cursor()

    default_city = preferred_city_for_user(st.session_state.user)
    city = st.selectbox("City", SUPPORTED_CITIES, index=city_index(default_city), key="schedule_lookup_city_v2")

    cursor.execute("""
        SELECT route_id, route_name, source, destination
        FROM routes
        WHERE status='active' AND city=%s
        ORDER BY route_name
    """, (city,))
    routes = cursor.fetchall()

    if not routes:
        st.warning(f"No active routes available in {city}.")
        cursor.close()
        return

    route_options = {f"{r[1]} ({r[2]} -> {r[3]})": r[0] for r in routes}
    selected_route = st.selectbox("Select Route", ["All Routes"] + list(route_options.keys()), key="schedule_lookup_route_v2")

    if selected_route == "All Routes":
        cursor.execute("""
            SELECT r.city, b.bus_number, r.route_name, r.source, r.destination, s.departure_time, s.arrival_time, s.days_of_week
            FROM schedules s
            JOIN buses b ON s.bus_id = b.bus_id
            JOIN routes r ON s.route_id = r.route_id
            WHERE s.status='active' AND r.city=%s
            ORDER BY r.route_name, s.departure_time
        """, (city,))
    else:
        route_id = route_options[selected_route]
        cursor.execute("""
            SELECT r.city, b.bus_number, r.route_name, r.source, r.destination, s.departure_time, s.arrival_time, s.days_of_week
            FROM schedules s
            JOIN buses b ON s.bus_id = b.bus_id
            JOIN routes r ON s.route_id = r.route_id
            WHERE s.route_id=%s AND s.status='active' AND r.city=%s
            ORDER BY s.departure_time
        """, (route_id, city))

    schedules = cursor.fetchall()
    if schedules:
        df = pd.DataFrame(schedules, columns=['City', 'Bus', 'Route', 'Source', 'Destination', 'Departure', 'Arrival', 'Days'])
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No schedules available for the selected filter.")

    cursor.close()


def manage_bookings():
    """Admin bookings view with city filter."""
    render_header("Booking Management")
    conn = get_connection()
    if not conn:
        return
    cursor = conn.cursor()

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        date_filter = st.date_input("Date", value=datetime.now().date(), key="admin_booking_date_v2")
    with col2:
        city_filter = st.selectbox("City", ["All Cities"] + SUPPORTED_CITIES, key="admin_booking_city_v2")
    with col3:
        status_filter = st.selectbox("Status", ["All", "Confirmed", "Cancelled"], key="admin_booking_status_v2")
    with col4:
        payment_filter = st.selectbox("Payment", ["All", "Completed", "Pending"], key="admin_booking_payment_v2")

    query = """
        SELECT b.booking_id, r.city, u.full_name, r.route_name, r.source, r.destination,
               b.booking_date, s.departure_time, b.num_passengers, b.total_fare,
               b.payment_status, b.booking_status, b.created_at
        FROM bookings b
        JOIN users u ON b.user_id = u.user_id
        JOIN schedules s ON b.schedule_id = s.schedule_id
        JOIN routes r ON s.route_id = r.route_id
        WHERE b.booking_date=%s
    """
    params = [date_filter]

    if city_filter != "All Cities":
        query += " AND r.city=%s"
        params.append(city_filter)
    if status_filter != "All":
        query += " AND b.booking_status=%s"
        params.append(status_filter.lower())
    if payment_filter != "All":
        query += " AND b.payment_status=%s"
        params.append(payment_filter.lower())

    query += " ORDER BY b.created_at DESC"
    cursor.execute(query, params)
    bookings = cursor.fetchall()

    if bookings:
        df = pd.DataFrame(bookings, columns=[
            'ID', 'City', 'Passenger', 'Route', 'From', 'To', 'Date', 'Time',
            'Passengers', 'Fare', 'Payment', 'Status', 'Booked At'
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Total Bookings", len(bookings))
        col_b.metric("Confirmed", sum(1 for b in bookings if b[11] == 'confirmed'))
        col_c.metric("Revenue", f"INR {sum(float(b[9]) for b in bookings if b[10] == 'completed'):.2f}")
    else:
        st.info("No bookings found for selected filters.")

    cursor.close()


def user_profile():
    """User profile management with preferred city."""
    render_header("My Profile")
    conn = get_connection()
    if not conn:
        return
    cursor = conn.cursor()

    cursor.execute("""
        SELECT username, full_name, email, phone, COALESCE(preferred_city, %s), created_at
        FROM users
        WHERE user_id=%s
    """, (DEFAULT_CITY, st.session_state.user['user_id']))
    user_data = cursor.fetchone()
    if not user_data:
        cursor.close()
        st.error("Unable to load profile.")
        return

    username, full_name, email, phone, preferred_city, created_at = user_data
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### Account Information")
        st.markdown(f"**Username:** {username}")
        st.markdown(f"**Full Name:** {full_name}")
        st.markdown(f"**Email:** {email}")
        st.markdown(f"**Phone:** {phone}")
        st.markdown(f"**Preferred City:** {preferred_city}")
        st.markdown(f"**Member Since:** {created_at.strftime('%B %d, %Y')}")

    with col2:
        st.markdown("#### Booking Statistics")
        cursor.execute("SELECT COUNT(*), COALESCE(SUM(total_fare), 0) FROM bookings WHERE user_id=%s", (st.session_state.user['user_id'],))
        total_bookings, total_spent = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM bookings WHERE user_id=%s AND booking_status='confirmed'", (st.session_state.user['user_id'],))
        active_bookings = cursor.fetchone()[0]
        st.metric("Total Bookings", total_bookings)
        st.metric("Active Bookings", active_bookings)
        st.metric("Total Spent", f"INR {float(total_spent):.2f}")

    st.divider()
    st.markdown("#### Update Profile")
    with st.form("update_profile_v2"):
        new_name = st.text_input("Full Name", value=full_name)
        new_email = st.text_input("Email", value=email)
        new_city = st.selectbox("Preferred City", SUPPORTED_CITIES, index=city_index(preferred_city), key="profile_city_v2")
        current_phone = phone.replace("+91", "") if phone else ""
        new_phone_number = st.text_input("Phone Number (+91)", value=current_phone, max_chars=10, key="profile_phone_v2")

        if st.form_submit_button("Update Profile", type="primary"):
            if new_phone_number and (len(new_phone_number) != 10 or not new_phone_number.isdigit()):
                st.error("Phone number must be exactly 10 digits.")
            else:
                try:
                    new_phone = f"+91{new_phone_number}" if new_phone_number else phone
                    cursor.execute("""
                        UPDATE users
                        SET full_name=%s, email=%s, phone=%s, preferred_city=%s
                        WHERE user_id=%s
                    """, (new_name, new_email, new_phone, new_city, st.session_state.user['user_id']))
                    conn.commit()
                    st.session_state.user['full_name'] = new_name
                    st.session_state.user['email'] = new_email
                    st.session_state.user['preferred_city'] = new_city
                    st.success("Profile updated successfully.")
                    time.sleep(0.5)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error updating profile: {e}")

    st.divider()
    st.markdown("#### Danger Zone")
    with st.expander("Delete Account", expanded=False):
        st.warning("This action cannot be undone.")
        delete_confirm = st.text_input("Type 'DELETE' to confirm account deletion")
        if st.button("Permanently Delete Account", type="primary", use_container_width=True, key="delete_account_btn_v2"):
            if delete_confirm == "DELETE":
                try:
                    cursor.execute("DELETE FROM payments WHERE booking_id IN (SELECT booking_id FROM bookings WHERE user_id=%s)", (st.session_state.user['user_id'],))
                    cursor.execute("DELETE FROM bookings WHERE user_id=%s", (st.session_state.user['user_id'],))
                    cursor.execute("DELETE FROM support_tickets WHERE user_id=%s", (st.session_state.user['user_id'],))
                    cursor.execute("DELETE FROM users WHERE user_id=%s", (st.session_state.user['user_id'],))
                    conn.commit()
                    logout_user()
                    st.success("Account deleted successfully.")
                    time.sleep(0.5)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error deleting account: {e}")
            else:
                st.error("Please type 'DELETE' exactly to confirm.")

    cursor.close()


def my_bookings():
    """User bookings with city visibility."""
    render_header("My Bookings")
    conn = get_connection()
    if not conn:
        return
    cursor = conn.cursor()

    cursor.execute("""
        SELECT b.booking_id, r.city, r.route_name, r.source, r.destination,
               b.booking_date, s.departure_time, b.num_passengers,
               b.total_fare, b.payment_status, b.booking_status
        FROM bookings b
        JOIN schedules s ON b.schedule_id = s.schedule_id
        JOIN routes r ON s.route_id = r.route_id
        WHERE b.user_id=%s
        ORDER BY b.booking_date DESC, s.departure_time DESC
    """, (st.session_state.user['user_id'],))
    bookings = cursor.fetchall()

    if bookings:
        df = pd.DataFrame(bookings, columns=[
            'ID', 'City', 'Route', 'From', 'To', 'Date', 'Time',
            'Passengers', 'Fare', 'Payment', 'Status'
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)

        booking_ids = [b[0] for b in bookings if b[10] == 'confirmed']
        if booking_ids:
            st.subheader("Cancel Booking")
            col1, col2, col3 = st.columns([2, 1, 1])
            with col1:
                cancel_booking_id = st.selectbox("Select Booking", booking_ids, key="cancel_booking_id_v2")
            with col2:
                confirm_cancel = st.checkbox("I confirm cancellation", key="confirm_cancel_v2")
            with col3:
                if st.button("Cancel Booking", type="secondary", use_container_width=True, disabled=not confirm_cancel, key="cancel_booking_btn_v2"):
                    try:
                        cursor.execute("""
                            UPDATE bookings
                            SET booking_status='cancelled'
                            WHERE booking_id=%s AND user_id=%s
                        """, (cancel_booking_id, st.session_state.user['user_id']))
                        conn.commit()
                        st.success("Booking cancelled.")
                        time.sleep(0.5)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")
    else:
        st.info("You have not made any bookings yet.")

    cursor.close()


def help_center():
    """Help center and FAQ."""
    render_header("Help Center")
    st.markdown("### Frequently Asked Questions")
    with st.expander("How do I book a ticket?"):
        st.markdown("""
        1. Open **Book Ticket**.
        2. Select your city, route, date, and bus.
        3. Confirm booking and payment.
        """)
    with st.expander("Can I cancel my booking?"):
        st.markdown("""
        1. Open **My Bookings**.
        2. Select a confirmed booking.
        3. Confirm cancellation.
        """)
    with st.expander("How is fare calculated?"):
        st.markdown("Fare is estimated by route segment and passenger count.")

    st.markdown("### Contact Support")
    st.markdown("""
    Phone: +91-1800-123-4567  
    Email: support@citybus.com  
    """)


def feedback_page():
    """User feedback page with robust validation."""
    render_header("Give Feedback")
    st.write("We value your feedback. Please rate your experience.")

    with st.form("feedback_form_v2"):
        selected = st.feedback("stars")
        comments = st.text_area("Comments", placeholder="Tell us what we can improve...")

        if st.form_submit_button("Submit Feedback", type="primary"):
            rating = (selected + 1) if selected is not None else None
            if rating is None or not comments.strip():
                st.warning("Please provide both a rating and comments.")
            else:
                try:
                    conn = get_connection()
                    if not conn:
                        st.error("Database is unavailable. Please try again.")
                    else:
                        cursor = conn.cursor()
                        cursor.execute("""
                            INSERT INTO feedback (user_id, rating, comments)
                            VALUES (%s, %s, %s)
                        """, (st.session_state.user['user_id'], rating, comments.strip()))
                        conn.commit()
                        cursor.close()
                        st.success("Thank you for your feedback.")
                        time.sleep(0.8)
                        st.rerun()
                except Exception as e:
                    st.error(f"Error submitting feedback: {e}")


def fare_calculator():
    """Deprecated: fare calculator removed from user menu."""
    render_header("Fare Calculator Removed")
    st.info("Fare Calculator has been removed. Use Book Ticket to get route-based fare automatically.")


def main():
    """Main application logic with secure admin routing and multi-city navigation."""
    init_page()

    if 'db_initialized' not in st.session_state:
        if init_database():
            st.session_state.db_initialized = True

    if not st.session_state.logged_in:
        if st.session_state.auth_page == 'register':
            register_page()
        else:
            login_page()
        return

    if st.session_state.user.get('role') == 'admin':
        verified, error_message = verify_admin_session()
        if not verified:
            st.error(error_message)
            logout_user()
            time.sleep(0.4)
            st.rerun()
            return

    with st.sidebar:
        st.markdown("### City Bus Portal")
        st.markdown(f"**{st.session_state.user['full_name']}**")
        st.caption(st.session_state.user['role'].title())
        if st.session_state.user.get('role') != 'admin':
            st.caption(f"Preferred City: {preferred_city_for_user(st.session_state.user)}")

        st.divider()

        default_page = st.session_state.quick_nav if st.session_state.get('quick_nav') else None
        st.session_state.quick_nav = None

        if st.session_state.user['role'] == 'admin':
            menu_options = ["📊 Dashboard", "📈 Analytics", "🚌 Buses", "🛣️ Routes", "⏰ Schedules", "📋 Bookings", "📑 Reports", "💬 Support", "⭐ Feedback"]
        else:
            menu_options = ["🎫 Book Ticket", "📋 My Bookings", "🛣️ View Routes", "⏰ Schedules", "👤 My Profile", "⭐ Feedback", "❓ Help"]

        menu_map = {opt: opt.split(" ", 1)[1] for opt in menu_options}
        if default_page:
            default_index = 0
            for i, opt in enumerate(menu_options):
                if default_page in opt:
                    default_index = i
                    break
        else:
            default_index = 0

        selected = st.radio("Menu", menu_options, index=default_index, label_visibility="collapsed")
        page = menu_map[selected]

        st.divider()
        if st.button("Logout", use_container_width=True):
            logout_user()
            st.rerun()

    if st.session_state.user['role'] == 'admin':
        if page == "Dashboard":
            admin_dashboard()
        elif page == "Analytics":
            analytics_dashboard()
        elif page == "Buses":
            manage_buses()
        elif page == "Routes":
            manage_routes()
        elif page == "Schedules":
            manage_schedules()
        elif page == "Bookings":
            manage_bookings()
        elif page == "Reports":
            admin_reports()
        elif page == "Support":
            admin_support()
        elif page == "Feedback":
            admin_feedback()
    else:
        if page == "Book Ticket":
            booking_system()
        elif page == "My Bookings":
            my_bookings()
        elif page == "View Routes":
            view_routes()
        elif page == "Schedules":
            schedule_lookup()
        elif page == "My Profile":
            user_profile()
        elif page == "Feedback":
            feedback_page()
        elif page == "Help":
            help_center()


if __name__ == "__main__":
    main()
