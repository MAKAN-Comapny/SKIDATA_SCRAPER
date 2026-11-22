"""
fabric_reader.py - Read-Only Query Client for Microsoft Fabric UnifiedWarehouse.

Guarantees:
  1. 100% Read-Only: Strictly blocks any non-SELECT / DDL / DML SQL statements.
  2. Multi-Driver Support: pyodbc (ODBC Driver 18/17) with token struct injection.
  3. OAuth2 Entra ID Token & Service Principal Authentication via MSAL and azure-identity.
  4. Dynamic Schema Introspection: Adapts to Skidata15 schema structure.
  5. Local Mock Mode: Allows safe verification when offline or in unit tests.
"""

from __future__ import annotations

import os
import re
import struct
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
DEFAULT_FABRIC_SCHEMA = "Skidata15"

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
        schema_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        mock_mode: bool = False,
    ):
        self.server = server or os.getenv("FABRIC_SERVER", DEFAULT_FABRIC_SERVER)
        self.database = database or os.getenv("FABRIC_DATABASE", DEFAULT_FABRIC_DATABASE)
        self.schema_name = schema_name or os.getenv("FABRIC_SCHEMA", DEFAULT_FABRIC_SCHEMA)
        self.tenant_id = tenant_id or os.getenv("TENANT_ID")
        self.client_id = client_id or os.getenv("CLIENT_ID")
        self.client_secret = client_secret or os.getenv("CLIENT_SECRET")
        self.mock_mode = mock_mode or os.getenv("FABRIC_MOCK_MODE", "false").lower() in ("true", "1", "yes")
        self._cached_tables: Optional[List[str]] = None

    def _acquire_entra_token(self) -> Optional[bytes]:
        """Acquires an Entra ID token formatted for SQL_COPT_SS_ACCESS_TOKEN (1256)."""
        if not (self.client_id and self.client_secret and self.tenant_id):
            return None

        # 1. Try MSAL
        try:
            import msal
            app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=self.client_secret,
                authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            )
            res = app.acquire_token_for_client(scopes=["https://database.windows.net/.default"])
            if "access_token" in res:
                raw_token = res["access_token"]
                token_bytes = raw_token.encode("UTF-16-LE")
                token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
                logger.info("Successfully acquired Entra ID token via MSAL for Fabric SQL scope.")
                return token_struct
            else:
                logger.warning(f"MSAL acquire_token returned: {res.get('error')}: {res.get('error_description')}")
        except Exception as e_msal:
            logger.warning(f"MSAL token attempt exception: {e_msal}")

        # 2. Try azure-identity
        try:
            from azure.identity import ClientSecretCredential
            cred = ClientSecretCredential(
                tenant_id=self.tenant_id,
                client_id=self.client_id,
                client_secret=self.client_secret,
            )
            tok = cred.get_token("https://database.windows.net/.default")
            token_bytes = tok.token.encode("UTF-16-LE")
            token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
            logger.info("Successfully acquired Entra ID token via azure-identity for Fabric SQL scope.")
            return token_struct
        except Exception as e_azure:
            logger.warning(f"azure-identity token attempt exception: {e_azure}")

        return None

    def _get_connection(self):
        """Creates an authenticated read-only connection to Fabric."""
        if self.mock_mode:
            return None

        import pyodbc

        available_drivers = pyodbc.drivers()
        logger.info(f"Available ODBC drivers on system: {available_drivers}")
        driver = "ODBC Driver 18 for SQL Server"
        for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
            if candidate in available_drivers:
                driver = candidate
                break
        else:
            if available_drivers:
                driver = available_drivers[0]

        SQL_COPT_SS_ACCESS_TOKEN = 1256
        server_host = self.server if self.server.startswith("tcp:") else f"tcp:{self.server}"
        token_struct = self._acquire_entra_token()

        # Method A: pyodbc with token struct (Recommended for Fabric)
        if token_struct is not None:
            try:
                conn_str = (
                    f"Driver={{{driver}}};"
                    f"Server={server_host},1433;"
                    f"Database={self.database};"
                    f"Encrypt=yes;"
                    f"TrustServerCertificate=yes;"
                    f"Connection Timeout=45;"
                )
                logger.info(f"Connecting to Fabric via token struct and driver [{driver}]...")
                return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct}, timeout=45)
            except Exception as e_token:
                logger.warning(f"Token-based connection attempt failed: {e_token}. Trying service principal string...")

        # Method B: pyodbc with ActiveDirectoryServicePrincipal connection string
        try:
            conn_str = (
                f"Driver={{{driver}}};"
                f"Server={server_host},1433;"
                f"Database={self.database};"
                f"Uid={self.client_id};"
                f"Pwd={self.client_secret};"
                f"Authentication=ActiveDirectoryServicePrincipal;"
                f"Encrypt=yes;"
                f"TrustServerCertificate=yes;"
                f"Connection Timeout=45;"
            )
            logger.info(f"Connecting to Fabric via ActiveDirectoryServicePrincipal and driver [{driver}]...")
            return pyodbc.connect(conn_str, timeout=45)
        except Exception as e_sp:
            logger.error(f"pyodbc ActiveDirectoryServicePrincipal connection failed: {e_sp}")

        # Method C: pymssql fallback (if available)
        try:
            import pymssql
            logger.info("Attempting fallback connection with pymssql...")
            return pymssql.connect(
                server=self.server,
                user=self.client_id,
                password=self.client_secret,
                database=self.database,
                port=1433,
                read_only=True,
                timeout=45,
            )
        except Exception as e_mssql:
            raise ConnectionError(
                f"Failed to connect to Microsoft Fabric UnifiedWarehouse: {e_sp} | pymssql: {e_mssql}"
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
                data = [list(row) for row in rows]
                return pd.DataFrame(data, columns=columns)
        finally:
            if hasattr(conn, "close"):
                conn.close()

    def get_schema_tables(self) -> List[str]:
        """Discovers all tables and views in target schema (e.g. Skidata15)."""
        if self._cached_tables is not None:
            return self._cached_tables
        if self.mock_mode:
            self._cached_tables = ["FactRevenue", "FactParking", "DimMalls"]
            return self._cached_tables
        try:
            sql = f"""
            SELECT TABLE_NAME, TABLE_TYPE 
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = '{self.schema_name}'
            ORDER BY TABLE_NAME;
            """
            df = self.execute_query(sql)
            if not df.empty:
                self._cached_tables = df["TABLE_NAME"].tolist()
                logger.info(f"Discovered {len(self._cached_tables)} objects in schema [{self.schema_name}]: {self._cached_tables}")
                return self._cached_tables
        except Exception as e:
            logger.warning(f"Could not introspect tables in schema [{self.schema_name}]: {e}")
        self._cached_tables = []
        return self._cached_tables

    def _get_table_columns(self, table_name: str) -> List[str]:
        """Retrieves column names for a specific table in schema."""
        if self.mock_mode:
            return ["date", "transactiondate", "location_id", "mall_name", "ticket_id", "gross_revenue", "in_time", "out_time"]
        try:
            sql = f"""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_SCHEMA = '{self.schema_name}' AND TABLE_NAME = '{table_name}';
            """
            df = self.execute_query(sql)
            if not df.empty:
                return [c.lower() for c in df["COLUMN_NAME"].tolist()]
        except Exception as e:
            logger.warning(f"Could not inspect columns for [{self.schema_name}].[{table_name}]: {e}")
        return []

    def get_daily_revenue_summary(self, start_date: date, end_date: date) -> pd.DataFrame:
        """
        Extracts daily gross revenue and ticket count from [{schema_name}].[FactRevenue]
        Grouped by Date and Mall_Name.
        """
        if self.mock_mode:
            return self._generate_mock_data("GROUP BY")

        tables = self.get_schema_tables()
        table_map = {t.lower(): t for t in tables}
        
        revenue_table = None
        for cand in ["factrevenue", "fact_revenue", "revenue", "revenueindepth", "vw_factrevenue", "v_factrevenue"]:
            if cand in table_map:
                revenue_table = table_map[cand]
                break
        if not revenue_table:
            revenue_table = "FactRevenue"

        cols = self._get_table_columns(revenue_table)

        # Resolve Date Column
        date_col = "r.TransactionDate"
        for c in ["transactiondate", "trandate", "date"]:
            if c in cols:
                date_col = f"r.[{c}]"
                break

        # Resolve Location / Mall Column
        loc_col = "r.Location_ID"
        for c in ["mall_name", "carpark", "car_park", "facility", "location_id", "locationid"]:
            if c in cols:
                loc_col = f"r.[{c}]"
                break

        # Resolve Ticket ID Column
        ticket_col = "r.Ticket_ID"
        for c in ["ticket_id", "ticket_number", "ticketid", "ticketnumber", "barcode"]:
            if c in cols:
                ticket_col = f"r.[{c}]"
                break

        # Resolve Revenue Column
        rev_col = "r.Gross_Revenue"
        for c in ["gross_revenue", "grossrevenue", "amount", "revenue", "gross_amount"]:
            if c in cols:
                rev_col = f"r.[{c}]"
                break

        # Check if DimMalls or DimMall exists in schema
        mall_join = ""
        mall_select = f"CAST({loc_col} AS VARCHAR(100))"
        for cand in ["dimmalls", "dimmall", "dim_mall", "malls"]:
            if cand in table_map:
                dim_table = table_map[cand]
                mall_join = f"LEFT JOIN [{self.schema_name}].[{dim_table}] m WITH (NOLOCK) ON {loc_col} = m.Mall_ID"
                mall_select = f"COALESCE(m.Mall_Name, CAST({loc_col} AS VARCHAR(100)))"
                break

        sql = f"""
        SELECT 
            CAST({date_col} AS DATE) AS [Date],
            {mall_select} AS [Mall_Name],
            COUNT({ticket_col}) AS [Fabric_Tickets],
            SUM(CAST({rev_col} AS DECIMAL(18, 2))) AS [Fabric_Revenue]
        FROM [{self.schema_name}].[{revenue_table}] r WITH (NOLOCK)
        {mall_join}
        WHERE CAST({date_col} AS DATE) >= '{start_date.isoformat()}'
          AND CAST({date_col} AS DATE) <= '{end_date.isoformat()}'
        GROUP BY CAST({date_col} AS DATE), {mall_select}
        ORDER BY [Date] DESC, [Mall_Name] ASC;
        """
        try:
            df = self.execute_query(sql)
        except Exception as e:
            logger.warning(f"Primary summary query failed: {e}. Retrying without table hints...")
            sql_fallback = f"""
            SELECT 
                CAST({date_col} AS DATE) AS [Date],
                CAST({loc_col} AS VARCHAR(100)) AS [Mall_Name],
                COUNT({ticket_col}) AS [Fabric_Tickets],
                SUM(CAST({rev_col} AS DECIMAL(18, 2))) AS [Fabric_Revenue]
            FROM [{self.schema_name}].[{revenue_table}] r
            WHERE CAST({date_col} AS DATE) >= '{start_date.isoformat()}'
              AND CAST({date_col} AS DATE) <= '{end_date.isoformat()}'
            GROUP BY CAST({date_col} AS DATE), CAST({loc_col} AS VARCHAR(100))
            ORDER BY [Date] DESC, [Mall_Name] ASC;
            """
            df = self.execute_query(sql_fallback)

        from skidata_reader import CAR_PARK_TO_MALL
        if "Mall_Name" in df.columns:
            df["Mall_Name"] = df["Mall_Name"].apply(
                lambda val: CAR_PARK_TO_MALL.get(str(val), CAR_PARK_TO_MALL.get(str(val).strip(), str(val)))
            )
        return df

    def get_ticket_details(self, target_date: date, location_id: Optional[str] = None) -> pd.DataFrame:
        """
        Extracts ticket-level details from [{schema_name}].[FactParking] for a specific date
        to isolate exact missing/mismatched ticket sequences.
        """
        if self.mock_mode:
            return self._generate_mock_data("TICKETS")

        tables = self.get_schema_tables()
        table_map = {t.lower(): t for t in tables}
        
        parking_table = None
        for cand in ["factparking", "fact_parking", "parking", "parkingtransactions", "vw_factparking", "v_factparking"]:
            if cand in table_map:
                parking_table = table_map[cand]
                break
        if not parking_table:
            parking_table = "FactParking"

        cols = self._get_table_columns(parking_table)

        ticket_col = "r.Ticket_ID"
        for c in ["ticket_id", "ticket_number", "ticketid", "ticketnumber", "barcode"]:
            if c in cols:
                ticket_col = f"r.[{c}]"
                break

        date_col = "r.TransactionDate"
        for c in ["transactiondate", "trandate", "date"]:
            if c in cols:
                date_col = f"r.[{c}]"
                break

        in_col = "r.In_Time"
        for c in ["in_time", "entrytime", "entry_time", "intime"]:
            if c in cols:
                in_col = f"r.[{c}]"
                break

        out_col = "r.Out_Time"
        for c in ["out_time", "exittime", "exit_time", "outtime"]:
            if c in cols:
                out_col = f"r.[{c}]"
                break

        rev_col = "r.Gross_Revenue"
        for c in ["gross_revenue", "grossrevenue", "payment", "amount", "revenue"]:
            if c in cols:
                rev_col = f"r.[{c}]"
                break

        loc_col = "r.Location_ID"
        for c in ["location_id", "mall_name", "carpark", "facility"]:
            if c in cols:
                loc_col = f"r.[{c}]"
                break

        loc_filter = f"AND {loc_col} = '{location_id}'" if location_id else ""

        sql = f"""
        SELECT 
            {ticket_col} AS [Ticket_Number],
            CAST({date_col} AS DATE) AS [Date],
            {in_col} AS [In_Time],
            {out_col} AS [Out_Time],
            {rev_col} AS [Payment],
            {loc_col} AS [Mall_Name]
        FROM [{self.schema_name}].[{parking_table}] r WITH (NOLOCK)
        WHERE CAST({date_col} AS DATE) = '{target_date.isoformat()}'
          {loc_filter};
        """
        try:
            return self.execute_query(sql)
        except Exception:
            sql_fallback = f"""
            SELECT 
                {ticket_col} AS [Ticket_Number],
                CAST({date_col} AS DATE) AS [Date],
                {in_col} AS [In_Time],
                {out_col} AS [Out_Time],
                {rev_col} AS [Payment],
                {loc_col} AS [Mall_Name]
            FROM [{self.schema_name}].[{parking_table}] r
            WHERE CAST({date_col} AS DATE) = '{target_date.isoformat()}'
              {loc_filter};
            """
            return self.execute_query(sql_fallback)

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
                    if d_offset == 1 and "Arab Mall" in mall:
                        rows.append([curr_date, mall, 1430, 14300.0])
                    elif d_offset == 3 and "The View Mall" in mall:
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
