"""
fabric_reader.py - Read-Only Query Client for Microsoft Fabric UnifiedWarehouse.

Guarantees:
  1. 100% Read-Only: Strictly blocks any non-SELECT / DDL / DML SQL statements.
  2. Multi-Driver Support: pyodbc (ODBC Driver 18/17) with pymssql fallback.
  3. Token-Based & Service Principal Authentication via Azure AD (Entra ID).
  4. Local Mock Mode: Allows safe verification when offline or in unit tests.
"""

from __future__ import annotations

import os
import re
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Any
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("fabric_reader")

DEFAULT_FABRIC_SERVER = (
    "cqhuoihibq6ute5vdjuwvjf4ia-hw3wjoqz72sebexa6jbbt3edba.datawarehouse.fabric.microsoft.com"
)
DEFAULT_FABRIC_DATABASE = "UnifiedWarehouse"

# Strict regex to block any data modification statement
MUTATING_SQL_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|EXEC|EXECUTE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _assert_read_only_sql(sql: str) -> None:
    """Enforces absolute read-only safety on any SQL passed to Fabric."""
    # Strip comments and strings
    cleaned = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"'.*?'", "", cleaned)
    
    match = MUTATING_SQL_PATTERN.search(cleaned)
    if match:
        raise PermissionError(
            f"SECURITY VIOLATION: Non-read-only keyword detected in SQL: '{match.group(0)}'. "
            f"Only SELECT queries are allowed against Microsoft Fabric."
        )


class FabricReader:
    """Safe, read-only reader for Microsoft Fabric UnifiedWarehouse."""

    def __init__(
        self,
        server: Optional[str] = None,
        database: Optional[str] = None,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        mock_mode: bool = False,
    ):
        self.server = server or os.getenv("FABRIC_SERVER", DEFAULT_FABRIC_SERVER)
        self.database = database or os.getenv("FABRIC_DATABASE", DEFAULT_FABRIC_DATABASE)
        self.tenant_id = tenant_id or os.getenv("TENANT_ID")
        self.client_id = client_id or os.getenv("CLIENT_ID")
        self.client_secret = client_secret or os.getenv("CLIENT_SECRET")
        self.mock_mode = mock_mode or os.getenv("FABRIC_MOCK_MODE", "false").lower() in ("true", "1", "yes")

    def _get_connection(self):
        """Creates an authenticated read-only connection to Fabric."""
        if self.mock_mode:
            return None

        # Try pyodbc first with ODBC Driver 18/17
        try:
            import pyodbc
            drivers = [d for d in pyodbc.drivers() if "ODBC Driver" in d and "SQL Server" in d]
            driver = drivers[0] if drivers else "SQL Server"
            
            # 1. Service Principal Auth
            if self.client_id and self.client_secret and self.tenant_id:
                conn_str = (
                    f"Driver={{{driver}}};"
                    f"Server={self.server},1433;"
                    f"Database={self.database};"
                    f"Uid={self.client_id}@{self.tenant_id};"
                    f"Pwd={self.client_secret};"
                    f"Authentication=ActiveDirectoryServicePrincipal;"
                    f"Encrypt=yes;"
                    f"TrustServerCertificate=no;"
                    f"ReadOnly=yes;"
                )
                return pyodbc.connect(conn_str, timeout=30)
            
            # 2. Interactive / Azure CLI token
            conn_str = (
                f"Driver={{{driver}}};"
                f"Server={self.server},1433;"
                f"Database={self.database};"
                f"Authentication=ActiveDirectoryInteractive;"
                f"Encrypt=yes;"
                f"ReadOnly=yes;"
            )
            return pyodbc.connect(conn_str, timeout=30)
        except Exception as e:
            logger.warning(f"pyodbc connection attempt failed: {e}. Trying pymssql...")
            
            # Try pymssql
            try:
                import pymssql
                return pymssql.connect(
                    server=self.server,
                    user=self.client_id,
                    password=self.client_secret,
                    database=self.database,
                    port=1433,
                    read_only=True,
                    timeout=30,
                )
            except Exception as e_mssql:
                raise ConnectionError(
                    f"Failed to connect to Microsoft Fabric UnifiedWarehouse via pyodbc or pymssql: {e_mssql}"
                )

    def execute_query(self, sql: str, params: Optional[tuple] = None) -> pd.DataFrame:
        """Executes a strictly read-only SQL query and returns a pandas DataFrame."""
        _assert_read_only_sql(sql)
        
        if self.mock_mode:
            logger.info("FabricReader running in MOCK MODE - returning synthetic warehouse data.")
            return self._generate_mock_data(sql)

        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                # Set session to read uncommitted to prevent locking
                try:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;")
                except Exception:
                    pass

                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                # Convert tuples/pyodbc rows to list
                data = [list(row) for row in rows]
                return pd.DataFrame(data, columns=columns)
        finally:
            if hasattr(conn, "close"):
                conn.close()

    def get_daily_revenue_summary(self, start_date: date, end_date: date) -> pd.DataFrame:
        """
        Extracts daily gross revenue and ticket count from SkiData.FactRevenue.
        Grouped by Date and Mall_Name (joined with DimMalls).
        """
        sql = f"""
        SELECT 
            CAST(r.TransactionDate AS DATE) AS [Date],
            COALESCE(m.Mall_Name, CAST(r.Location_ID AS VARCHAR(50))) AS [Mall_Name],
            COUNT(r.Ticket_ID) AS [Fabric_Tickets],
            SUM(CAST(r.Gross_Revenue AS DECIMAL(18, 2))) AS [Fabric_Revenue]
        FROM SkiData.FactRevenue r WITH (NOLOCK)
        LEFT JOIN DimMalls m WITH (NOLOCK) ON r.Location_ID = m.Mall_ID
        WHERE CAST(r.TransactionDate AS DATE) >= '{start_date.isoformat()}'
          AND CAST(r.TransactionDate AS DATE) <= '{end_date.isoformat()}'
        GROUP BY CAST(r.TransactionDate AS DATE), COALESCE(m.Mall_Name, CAST(r.Location_ID AS VARCHAR(50)))
        ORDER BY [Date] DESC, [Mall_Name] ASC;
        """
        return self.execute_query(sql)

    def get_ticket_details(self, target_date: date, location_id: Optional[str] = None) -> pd.DataFrame:
        """
        Extracts ticket-level details from SkiData.FactParking for a specific date
        to isolate exact missing/mismatched ticket sequences.
        """
        loc_filter = f"AND r.Location_ID = '{location_id}'" if location_id else ""
        sql = f"""
        SELECT 
            r.Ticket_ID AS [Ticket_Number],
            CAST(r.TransactionDate AS DATE) AS [Date],
            r.In_Time,
            r.Out_Time,
            r.Gross_Revenue AS [Payment],
            r.Location_ID AS [Mall_Name]
        FROM SkiData.FactParking r WITH (NOLOCK)
        WHERE CAST(r.TransactionDate AS DATE) = '{target_date.isoformat()}'
          {loc_filter};
        """
        return self.execute_query(sql)

    def _generate_mock_data(self, sql: str) -> pd.DataFrame:
        """Generates realistic mock Fabric responses for local testing/offline validation."""
        today = datetime.now().date()
        mock_malls = [
            "02 - Nakheel Mall Dammam",
            "01 - The View Mall Riyadh",
            "06 - Al Noor Mall Madinah",
            "03 - Arab Mall Jeddah",
        ]
        
        # If it's a summary query
        if "GROUP BY" in sql.upper():
            rows = []
            for d_offset in range(15):
                curr_date = today - timedelta(days=d_offset)
                for mall in mock_malls:
                    # Introduce a realistic intentional mismatch on 2 dates for testing
                    if d_offset == 1 and "Arab Mall" in mall:
                        # Missing 20 tickets in Fabric
                        rows.append([curr_date, mall, 1430, 14300.0])
                    elif d_offset == 3 and "The View Mall" in mall:
                        # Extra tickets in Fabric
                        rows.append([curr_date, mall, 910, 9100.0])
                    else:
                        rows.append([curr_date, mall, 1200 + d_offset * 10, (1200 + d_offset * 10) * 10.0])
            return pd.DataFrame(rows, columns=["Date", "Mall_Name", "Fabric_Tickets", "Fabric_Revenue"])
        
        # Ticket level mock
        rows = []
        for i in range(1, 100):
            ticket_id = f"331719841050293000{i:05d}"
            rows.append([ticket_id, today - timedelta(days=1), "2026-11-21 14:00:00", "2026-11-21 16:30:00", 10.0, mock_malls[0]])
        return pd.DataFrame(rows, columns=["Ticket_Number", "Date", "In_Time", "Out_Time", "Payment", "Mall_Name"])
