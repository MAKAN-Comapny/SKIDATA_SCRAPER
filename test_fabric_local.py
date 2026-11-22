"""
test_fabric_local.py - Standalone Local Test Script to Fetch Data directly from Microsoft Fabric.
"""

import os
import sys
import pyodbc
import pandas as pd
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

SERVER = os.getenv("FABRIC_SERVER", "cqhuoihibq6ute5vdjuwvjf4ia-hw3wjoqz72sebexa6jbbt3edba.datawarehouse.fabric.microsoft.com")
DATABASE = os.getenv("FABRIC_DATABASE", "UnifiedWarehouse")
SCHEMA = os.getenv("FABRIC_SCHEMA", "Skidata15")
USER = os.getenv("FABRIC_USER") or os.getenv("FABRIC_USERNAME")
PASSWORD = os.getenv("FABRIC_PASSWORD")

print("=" * 65)
print("🔌 LOCAL TEST: FETCHING DATA DIRECTLY FROM MICROSOFT FABRIC")
print("=" * 65)
print(f"Server:   {SERVER}")
print(f"Database: {DATABASE}")
print(f"Schema:   {SCHEMA}")
print(f"User:     {USER if USER else '⚠️ NOT SET in .env (Add FABRIC_USER=...)'}")
print(f"Password: {'*' * len(PASSWORD) if PASSWORD else '⚠️ NOT SET in .env (Add FABRIC_PASSWORD=...)'}")
print("=" * 65)

if not (USER and PASSWORD):
    print("\n❌ Error: Please provide FABRIC_USER and FABRIC_PASSWORD in .env or run with:")
    print("   $env:FABRIC_USER='...'; $env:FABRIC_PASSWORD='...'; uv run python test_fabric_local.py")
    exit(1)

# Find ODBC Driver
drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
print(f"Available Drivers on this machine: {drivers}")
driver = "ODBC Driver 18 for SQL Server" if any("ODBC Driver 18" in d for d in drivers) else (drivers[0] if drivers else "SQL Server")
print(f"Selected Driver: [{driver}]")

# Attempt 1: ActiveDirectoryPassword (Power BI / Excel method)
conn = None
for auth_mode in ["ActiveDirectoryPassword", "SqlPassword"]:
    for trust_cert in ["yes", "no"]:
        try:
            print(f"\nAttempting connection with {auth_mode} (TrustServerCertificate={trust_cert})...")
            if auth_mode == "ActiveDirectoryPassword":
                conn_str = (
                    f"Driver={{{driver}}};"
                    f"Server=tcp:{SERVER},1433;"
                    f"Database={DATABASE};"
                    f"Uid={USER};"
                    f"Pwd={PASSWORD};"
                    f"Authentication=ActiveDirectoryPassword;"
                    f"Encrypt=yes;"
                    f"TrustServerCertificate={trust_cert};"
                    f"Connection Timeout=30;"
                )
            else:
                conn_str = (
                    f"Driver={{{driver}}};"
                    f"Server=tcp:{SERVER},1433;"
                    f"Database={DATABASE};"
                    f"Uid={USER};"
                    f"Pwd={PASSWORD};"
                    f"Encrypt=yes;"
                    f"TrustServerCertificate={trust_cert};"
                    f"Connection Timeout=30;"
                )
            conn = pyodbc.connect(conn_str, timeout=30)
            print(f"🎉 SUCCESS: Connected to Fabric via {auth_mode}!")
            break
        except Exception as e:
            print(f"⚠️ {auth_mode} (TrustCert={trust_cert}) failed: {e}")
    if conn:
        break

if not conn:
    print("\n❌ All connection attempts failed. Check username/password.")
    exit(1)

# Query 1: Discover Tables & Views in Skidata15
print("\n" + "=" * 65)
print(f"📋 DISCOVERING OBJECTS IN SCHEMA [{SCHEMA}]")
print("=" * 65)
tables_df = pd.read_sql(f"""
    SELECT TABLE_NAME, TABLE_TYPE 
    FROM INFORMATION_SCHEMA.TABLES 
    WHERE TABLE_SCHEMA = '{SCHEMA}'
    ORDER BY TABLE_NAME;
""", conn)
print(tables_df.to_string(index=False))

# Query 2: Sample Rows from FactRevenue or first table
target_table = "FactRevenue" if "FactRevenue" in tables_df["TABLE_NAME"].values else tables_df["TABLE_NAME"].iloc[0]
print("\n" + "=" * 65)
print(f"📊 FETCHING FIRST 5 ROWS FROM [{SCHEMA}].[{target_table}]")
print("=" * 65)
sample_df = pd.read_sql(f"SELECT TOP 5 * FROM [{SCHEMA}].[{target_table}];", conn)
print(sample_df.to_string())

conn.close()
print("\n✅ Local fetch test completed successfully!")
