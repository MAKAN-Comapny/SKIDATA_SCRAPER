"""
fabric_reader.py - Read-Only Query Client for Microsoft Fabric UnifiedWarehouse.

Guarantees:
  1. 100% Read-Only: Strictly blocks any non-SELECT / DDL / DML SQL statements.
  2. Multi-Driver Support: pyodbc (ODBC Driver 18/17) with token struct injection.
  3. OAuth2 Entra ID Token & Service Principal Authentication via MSAL and azure-identity.
     Supports both Power BI Fabric scope (https://analysis.windows.net/powerbi/api/.default)
     and Azure SQL scope (https://database.windows.net/.default).
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

# Primary scope for Microsoft Fabric / Power BI SQL endpoints, followed by Azure SQL
FABRIC_SCOPES = [
    "https://analysis.windows.net/powerbi/api/.default",
    "https://database.windows.net/.default",
]

# Strict regex to block any data modification statement
MUTATING_SQL_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|EXEC|EXECUTE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _assert_read_only_sql(sql: str) -> None:
    """Enforces absolute read-only safety on any SQL passed to Fabric."""
    cleaned = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"'.*?'", "", cleaned)
    
    match = MUTATING_SQL_PATTERN.search(cleaned)
    if match:
        raise PermissionError(
            f"SECURITY VIOLATION: Non-read-only keyword detected in SQL: '{match.group(0)}'. "
            f"Only SELECT queries are allowed against Microsoft Fabric."
        )


def _match_column(cols: List[str], candidates: List[str]) -> Optional[str]:
    """
    Matches candidate column names against introspected table columns.
    Normalizes whitespace, underscores, hyphens, and case so e.g. 'Car Park'
    matches 'carpark', 'car_park', or 'Car Park'.
    """
    if not cols:
        return None
    norm_map = {}
    for c in cols:
        norm = re.sub(r"[^a-z0-9]", "", str(c).lower())
        if norm and norm not in norm_map:
            norm_map[norm] = c

    for cand in candidates:
        norm_cand = re.sub(r"[^a-z0-9]", "", cand.lower())
        if norm_cand in norm_map:
            return norm_map[norm_cand]
    return None


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
        username: Optional[str] = None,
        password: Optional[str] = None,
        mock_mode: bool = False,
    ):
        self.server = server or os.getenv("FABRIC_SERVER", DEFAULT_FABRIC_SERVER)
        self.database = database or os.getenv("FABRIC_DATABASE", DEFAULT_FABRIC_DATABASE)
        self.schema_name = schema_name or os.getenv("FABRIC_SCHEMA", DEFAULT_FABRIC_SCHEMA)
        self.tenant_id = tenant_id or os.getenv("TENANT_ID")
        self.client_id = client_id or os.getenv("CLIENT_ID")
        self.client_secret = client_secret or os.getenv("CLIENT_SECRET")
        self.username = username or os.getenv("FABRIC_USER") or os.getenv("FABRIC_USERNAME")
        self.password = password or os.getenv("FABRIC_PASSWORD")
        self.mock_mode = mock_mode or os.getenv("FABRIC_MOCK_MODE", "false").lower() in ("true", "1", "yes")
        self._cached_tables: Optional[List[str]] = None

    def _acquire_entra_tokens(self) -> List[tuple[str, bytes]]:
        """
        Acquires Entra ID tokens across Fabric and Azure SQL scopes.
        Returns list of (scope_name, packed_token_struct).
        """
        if not (self.client_id and self.client_secret and self.tenant_id):
            return []

        tokens: List[tuple[str, bytes]] = []

        # 1. Try MSAL for each scope
        for scope in FABRIC_SCOPES:
            try:
                import msal
                app = msal.ConfidentialClientApplication(
                    client_id=self.client_id,
                    client_credential=self.client_secret,
                    authority=f"https://login.microsoftonline.com/{self.tenant_id}",
                )
                res = app.acquire_token_for_client(scopes=[scope])
                if "access_token" in res:
                    raw_token = res["access_token"]
                    token_bytes = raw_token.encode("UTF-16-LE")
                    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
                    logger.info(f"Acquired Entra ID token via MSAL for scope: {scope}")
                    tokens.append((scope, token_struct))
                else:
                    logger.debug(f"MSAL scope {scope} returned: {res.get('error')}")
            except Exception as e_msal:
                logger.debug(f"MSAL scope {scope} exception: {e_msal}")

        # 2. Try azure-identity fallback
        if not tokens:
            try:
                from azure.identity import ClientSecretCredential
                cred = ClientSecretCredential(
                    tenant_id=self.tenant_id,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                )
                for scope in FABRIC_SCOPES:
                    try:
                        tok = cred.get_token(scope)
                        token_bytes = tok.token.encode("UTF-16-LE")
                        token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
                        logger.info(f"Acquired Entra ID token via azure-identity for scope: {scope}")
                        tokens.append((scope, token_struct))
                    except Exception as e_tok:
                        logger.debug(f"azure-identity scope {scope} failed: {e_tok}")
            except Exception as e_azure:
                logger.debug(f"azure-identity init failed: {e_azure}")

        return tokens

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
        clean_server = self.server.replace("tcp:", "").strip()
        server_host = clean_server

        # Method 0: Direct User Login & Password (Power BI / Excel method)
        if self.username and self.password:
            logger.info(f"Connecting to Fabric using user credentials (UID={self.username})...")
            for auth_mode in ["ActiveDirectoryPassword", "SqlPassword"]:
                for trust_cert in ["yes", "no"]:
                    try:
                        if auth_mode == "ActiveDirectoryPassword":
                            conn_str = (
                                f"Driver={{{driver}}};"
                                f"Server=tcp:{server_host},1433;"
                                f"Database={self.database};"
                                f"Uid={self.username};"
                                f"Pwd={self.password};"
                                f"Authentication=ActiveDirectoryPassword;"
                                f"Encrypt=yes;"
                                f"TrustServerCertificate={trust_cert};"
                                f"Connection Timeout=30;"
                            )
                        else:
                            conn_str = (
                                f"Driver={{{driver}}};"
                                f"Server=tcp:{server_host},1433;"
                                f"Database={self.database};"
                                f"Uid={self.username};"
                                f"Pwd={self.password};"
                                f"Encrypt=yes;"
                                f"TrustServerCertificate={trust_cert};"
                                f"Connection Timeout=30;"
                            )
                        return pyodbc.connect(conn_str, timeout=30)
                    except Exception as e_user_auth:
                        logger.warning(f"User login attempt ({auth_mode}, TrustCert={trust_cert}) failed: {e_user_auth}")

        token_list = self._acquire_entra_tokens()

        # Method A: Try pyodbc with each acquired Entra ID token struct
        last_token_error = None
        for scope, token_struct in token_list:
            for trust_cert in ("yes", "no"):
                try:
                    conn_str = (
                        f"Driver={{{driver}}};"
                        f"Server=tcp:{server_host},1433;"
                        f"Database={self.database};"
                        f"Encrypt=yes;"
                        f"TrustServerCertificate={trust_cert};"
                        f"Connection Timeout=30;"
                    )
                    logger.info(f"Connecting to Fabric using token ({scope}, TrustCert={trust_cert})...")
                    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct}, timeout=30)
                except Exception as e_tok_conn:
                    last_token_error = e_tok_conn
                    logger.warning(f"Token connection failed for scope {scope} (TrustCert={trust_cert}): {e_tok_conn}")

        # Method B: pyodbc with ActiveDirectoryServicePrincipal connection string
        e_sp = None
        for uid_format in [self.client_id, f"{self.client_id}@{self.tenant_id}"]:
            try:
                conn_str = (
                    f"Driver={{{driver}}};"
                    f"Server=tcp:{server_host},1433;"
                    f"Database={self.database};"
                    f"Uid={uid_format};"
                    f"Pwd={self.client_secret};"
                    f"Authentication=ActiveDirectoryServicePrincipal;"
                    f"Encrypt=yes;"
                    f"TrustServerCertificate=yes;"
                    f"Connection Timeout=30;"
                )
                logger.info(f"Connecting to Fabric via ActiveDirectoryServicePrincipal (UID={uid_format[:8]}...)...")
                return pyodbc.connect(conn_str, timeout=30)
            except Exception as e_sp_attempt:
                e_sp = e_sp_attempt
                logger.warning(f"pyodbc ActiveDirectoryServicePrincipal failed (UID={uid_format[:8]}...): {e_sp_attempt}")

        # Method C: pymssql fallback (if available)
        e_mssql = None
        try:
            import pymssql
            logger.info("Attempting fallback connection with pymssql...")
            return pymssql.connect(
                server=server_host,
                user=self.client_id,
                password=self.client_secret,
                database=self.database,
                port=1433,
                read_only=True,
                timeout=30,
            )
        except Exception as e_mssql_attempt:
            e_mssql = e_mssql_attempt
            logger.warning(f"pymssql fallback attempt failed: {e_mssql_attempt}")

        error_details = (
            f"Token Error: {last_token_error} | "
            f"Service Principal Error: {e_sp} | "
            f"pymssql Error: {e_mssql}"
        )
        raise ConnectionError(
            f"Failed to connect to Microsoft Fabric UnifiedWarehouse: {error_details}"
        )

    def test_connection(self) -> bool:
        """Quick healthcheck to verify Fabric connectivity and introspect schema in < 5 seconds."""
        try:
            tables = self.get_schema_tables()
            logger.info(f"Verified connection to Fabric! Total objects in [{self.schema_name}]: {len(tables)}")
            # Sample FactRid / Revenue table
            for rev_t in ["FactRid", "fact_rid", "FactRevenue", "fact_revenue"]:
                if rev_t in tables:
                    cols = self._get_table_columns(rev_t)
                    logger.info(f"Target Revenue Table [{self.schema_name}].[{rev_t}] Columns: {cols}")
                    sample = self.execute_query(f"SELECT TOP 2 * FROM [{self.schema_name}].[{rev_t}];")
                    logger.info(f"Sample from [{rev_t}]:\n{sample.to_string()}")
                    break
            # Sample FactParking
            for park_t in ["FactParking", "fact_parking"]:
                if park_t in tables:
                    cols = self._get_table_columns(park_t)
                    logger.info(f"Target Parking Table [{self.schema_name}].[{park_t}] Columns: {cols}")
                    sample = self.execute_query(f"SELECT TOP 2 * FROM [{self.schema_name}].[{park_t}];")
                    logger.info(f"Sample from [{park_t}]:\n{sample.to_string()}")
                    break
            return True
        except Exception as e:
            logger.error(f"Fabric test_connection failed: {e}")
            raise

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
        """Retrieves column names for a specific table in schema using multi-layer introspection."""
        if self.mock_mode:
            return [
                "date", "car park", "facility", "quantity", "theoretical revenue",
                "gross revenue", "payment", "serial number", "entry date/time", "exit date/time"
            ]

        conn = self._get_connection()
        if not conn:
            return []

        # Layer 1: Direct SELECT TOP 0 * (Instant, works in Fabric, returns exact column names via cursor.description)
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT TOP 0 * FROM [{self.schema_name}].[{table_name}];")
                if cursor.description:
                    cols = [desc[0] for desc in cursor.description]
                    logger.info(f"Introspected {len(cols)} columns for [{self.schema_name}].[{table_name}] via TOP 0: {cols}")
                    return cols
        except Exception as e_top0:
            logger.debug(f"TOP 0 column introspection failed for [{table_name}]: {e_top0}")

        # Layer 2: sys.columns via OBJECT_ID
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"""
                    SELECT c.name 
                    FROM sys.columns c
                    WHERE c.object_id = OBJECT_ID('[{self.schema_name}].[{table_name}]')
                    ORDER BY c.column_id;
                """)
                rows = cursor.fetchall()
                if rows:
                    cols = [r[0] for r in rows]
                    logger.info(f"Introspected {len(cols)} columns for [{self.schema_name}].[{table_name}] via sys.columns: {cols}")
                    return cols
        except Exception as e_sys:
            logger.debug(f"sys.columns introspection failed for [{table_name}]: {e_sys}")

        # Layer 3: INFORMATION_SCHEMA (case-insensitive)
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"""
                    SELECT COLUMN_NAME 
                    FROM INFORMATION_SCHEMA.COLUMNS 
                    WHERE LOWER(TABLE_SCHEMA) = LOWER('{self.schema_name}')
                      AND LOWER(TABLE_NAME) = LOWER('{table_name}')
                    ORDER BY ORDINAL_POSITION;
                """)
                rows = cursor.fetchall()
                if rows:
                    cols = [r[0] for r in rows]
                    logger.info(f"Introspected {len(cols)} columns for [{self.schema_name}].[{table_name}] via INFORMATION_SCHEMA: {cols}")
                    return cols
        except Exception as e_info:
            logger.debug(f"INFORMATION_SCHEMA column introspection failed for [{table_name}]: {e_info}")

        return []

    def get_daily_revenue_summary(self, start_date: date, end_date: date) -> pd.DataFrame:
        """
        Extracts daily gross revenue and ticket count from [{schema_name}].[FactRid] / FactRevenue
        Grouped by Date and Mall_Name.
        """
        if self.mock_mode:
            return self._generate_mock_data("GROUP BY")

        tables = self.get_schema_tables()
        table_map = {t.lower(): t for t in tables}
        
        revenue_table = None
        for cand in ["factrid", "fact_rid", "dimticketrevenue", "dim_ticket_revenue", "factrevenue", "fact_revenue", "revenue", "revenueindepth"]:
            if cand in table_map:
                revenue_table = table_map[cand]
                break
        if not revenue_table:
            revenue_table = "FactRid" if "FactRid" in tables else "FactRevenue"

        cols = self._get_table_columns(revenue_table)

        # 1. Resolve Date Column
        date_c = _match_column(cols, [
            "date", "transactiondate", "transaction_date", "trandate", 
            "datekey", "entry_date_time", "entry_datetime", "created_at"
        ])
        if date_c:
            if "datekey" in date_c.lower():
                date_expr = f"CONVERT(DATE, CAST(r.[{date_c}] AS VARCHAR(8)), 112)"
            else:
                date_expr = f"CAST(r.[{date_c}] AS DATE)"
        else:
            date_expr = "CAST(r.[Date] AS DATE)"

        # 2. Resolve Mall / Location Column
        loc_c = _match_column(cols, [
            "car park", "carpark", "car_park", "facility", 
            "mall_name", "location_id", "locationid", "location_name"
        ])
        loc_col_name = loc_c or "Car Park"
        mall_select = f"CAST(r.[{loc_col_name}] AS VARCHAR(100))"

        # Check if DimLocation should be joined (if loc_c appears to be an ID code)
        mall_join = ""
        if loc_c and any(term in loc_c.lower() for term in ["id", "code"]):
            for cand in ["dimlocation", "dim_location", "dimmalls", "dimmall", "dim_mall", "malls"]:
                if cand in table_map:
                    dim_table = table_map[cand]
                    dim_cols = self._get_table_columns(dim_table)
                    dim_id = _match_column(dim_cols, ["location_id", "locationid", "mall_id", "id", "carpark_id"]) or loc_c
                    dim_nm = _match_column(dim_cols, ["mall_name", "location_name", "locationname", "carpark_name", "name", "facility"])
                    if dim_nm:
                        mall_join = f"LEFT JOIN [{self.schema_name}].[{dim_table}] m ON r.[{loc_c}] = m.[{dim_id}]"
                        mall_select = f"COALESCE(CAST(m.[{dim_nm}] AS VARCHAR(100)), CAST(r.[{loc_c}] AS VARCHAR(100)))"
                    break

        # 3. Resolve Tickets / Quantity Column
        qty_c = _match_column(cols, ["quantity", "qty", "tickets", "ticket_count"])
        ticket_id_c = _match_column(cols, [
            "serial number", "serial_number", "ticket_id", "ticket_number", 
            "ticketid", "barcode", "access id", "access_id"
        ])
        if qty_c:
            ticket_expr = f"SUM(CAST(r.[{qty_c}] AS BIGINT))"
        elif ticket_id_c:
            ticket_expr = f"COUNT(r.[{ticket_id_c}])"
        else:
            ticket_expr = "COUNT(*)"

        # 4. Resolve Revenue Column
        rev_c = _match_column(cols, [
            "gross_revenue", "gross revenue", "grossrevenue",
            "theoretical revenue", "theoretical_revenue",
            "payment", "amount", "revenue", "gross_amount", "total_amount", "net_revenue"
        ])
        rev_col_name = rev_c or "Gross Revenue"
        rev_expr = f"SUM(CAST(r.[{rev_col_name}] AS DECIMAL(18, 2)))"

        # Strictly NO 'WITH (NOLOCK)' (illegal in Microsoft Fabric)
        sql = f"""
        SELECT 
            {date_expr} AS [Date],
            {mall_select} AS [Mall_Name],
            {ticket_expr} AS [Fabric_Tickets],
            {rev_expr} AS [Fabric_Revenue]
        FROM [{self.schema_name}].[{revenue_table}] r
        {mall_join}
        WHERE {date_expr} >= '{start_date.isoformat()}'
          AND {date_expr} <= '{end_date.isoformat()}'
        GROUP BY {date_expr}, {mall_select}
        ORDER BY [Date] DESC, [Mall_Name] ASC;
        """
        logger.info(f"Executing Fabric Revenue Summary SQL:\n{sql}")
        try:
            df = self.execute_query(sql)
        except Exception as e:
            logger.warning(f"Primary summary query failed: {e}. Retrying without JOIN...")
            sql_fallback = f"""
            SELECT 
                {date_expr} AS [Date],
                CAST(r.[{loc_col_name}] AS VARCHAR(100)) AS [Mall_Name],
                {ticket_expr} AS [Fabric_Tickets],
                {rev_expr} AS [Fabric_Revenue]
            FROM [{self.schema_name}].[{revenue_table}] r
            WHERE {date_expr} >= '{start_date.isoformat()}'
              AND {date_expr} <= '{end_date.isoformat()}'
            GROUP BY {date_expr}, CAST(r.[{loc_col_name}] AS VARCHAR(100))
            ORDER BY [Date] DESC, [Mall_Name] ASC;
            """
            logger.info(f"Executing Fallback Fabric Revenue Summary SQL:\n{sql_fallback}")
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

        ticket_c = _match_column(cols, ["serial number", "serial_number", "ticket_id", "ticket_number", "ticketid", "barcode", "access id", "access_id"]) or "Serial Number"
        date_c = _match_column(cols, ["date", "entry date/time", "entry_date_time", "entry date", "transaction date/time", "transactiondate", "trandate", "datekey"]) or "Date"
        in_c = _match_column(cols, ["entry date/time", "entry_date_time", "in_time", "entrytime", "entry_time", "intime"]) or ticket_c
        out_c = _match_column(cols, ["exit date/time", "exit_date_time", "out_time", "exittime", "exit_time", "outtime"]) or ticket_c
        rev_c = _match_column(cols, ["payment", "gross_revenue", "grossrevenue", "amount", "revenue", "discount amount"]) or "Payment"
        loc_c = _match_column(cols, ["car park", "carpark", "car_park", "facility", "mall_name", "location_id"]) or "Car Park"

        if "datekey" in date_c.lower():
            date_filter = f"CONVERT(DATE, CAST(r.[{date_c}] AS VARCHAR(8)), 112) = '{target_date.isoformat()}'"
        else:
            date_filter = f"CAST(r.[{date_c}] AS DATE) = '{target_date.isoformat()}'"

        loc_filter = f"AND CAST(r.[{loc_c}] AS VARCHAR(100)) = '{location_id}'" if location_id else ""

        # Strictly NO 'WITH (NOLOCK)' (illegal in Microsoft Fabric)
        sql = f"""
        SELECT 
            CAST(r.[{ticket_c}] AS VARCHAR(100)) AS [Ticket_Number],
            CAST(r.[{date_c}] AS DATE) AS [Date],
            CAST(r.[{in_c}] AS VARCHAR(50)) AS [In_Time],
            CAST(r.[{out_c}] AS VARCHAR(50)) AS [Out_Time],
            CAST(r.[{rev_c}] AS DECIMAL(18, 2)) AS [Payment],
            CAST(r.[{loc_c}] AS VARCHAR(100)) AS [Mall_Name]
        FROM [{self.schema_name}].[{parking_table}] r
        WHERE {date_filter}
          {loc_filter};
        """
        logger.info(f"Executing Fabric Ticket Details SQL:\n{sql}")
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
        
        rows = []
        for i in range(1, 100):
            ticket_id = f"331719841050293000{i:05d}"
            rows.append([ticket_id, today - timedelta(days=1), "2026-11-21 14:00:00", "2026-11-21 16:30:00", 10.0, mock_malls[0]])
        return pd.DataFrame(rows, columns=["Ticket_Number", "Date", "In_Time", "Out_Time", "Payment", "Mall_Name"])
