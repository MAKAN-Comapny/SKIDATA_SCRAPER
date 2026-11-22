"""
notifier.py - Outlook Email Dispatcher & HTML Formatter for Reconciliation Reports.

Features:
  1. Professional HTML report with inline CSS optimized for Outlook (Desktop, Web, Mobile).
  2. Summary Inconsistency Matrix + Detailed Ticket Breakdowns (In SkiData missing in Fabric, etc.).
  3. Microsoft Graph API /sendMail dispatch (OAuth2 Service Principal) with SMTP fallback.
  4. Restricted recipient routing: Exclusively targets specified inboxes (Sumit & Karim).
  5. Local preview: Can write HTML report to disk for browser verification before sending.
"""

from __future__ import annotations

import base64
import logging
import os
import smtplib
from datetime import datetime
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from typing import List, Optional
import requests
from dotenv import load_dotenv

from reconcile_engine import ReconciliationResult

load_dotenv()
logger = logging.getLogger("notifier")

# Default recipients (Sumit & Karim)
DEFAULT_RECIPIENTS = ["sumit.dhankhar@makan.sa", "karim.essam@makan.sa"]


def get_recipients() -> List[str]:
    """Retrieves recipient email list from environment or defaults."""
    env_recipients = os.getenv("RECON_ALERT_RECIPIENTS")
    if env_recipients:
        return [r.strip() for r in env_recipients.split(",") if r.strip()]
    return DEFAULT_RECIPIENTS


def build_html_report(result: ReconciliationResult) -> str:
    """Generates the formatted HTML body for Microsoft Outlook."""
    status_color = "#28a745" if result.total_mismatches == 0 else "#dc3545"
    status_text = "ALL IN SYNC (100% MATCH)" if result.total_mismatches == 0 else f"{result.total_mismatches} INCONSISTENCIES FOUND"

    # Build Master Summary Table Rows
    summary_rows_html = []
    for _, row in result.summary_df.iterrows():
        status = row["Status"]
        if status == "MATCH":
            badge = '<span style="background-color:#e8f5e9; color:#2e7d32; padding:3px 8px; border-radius:4px; font-weight:bold; font-size:11px;">MATCH</span>'
            row_bg = "#ffffff"
        elif status == "MISSING_IN_FABRIC":
            badge = '<span style="background-color:#ffebee; color:#c62828; padding:3px 8px; border-radius:4px; font-weight:bold; font-size:11px;">MISSING IN FABRIC</span>'
            row_bg = "#fff8f8"
        elif status == "MISSING_IN_SKIDATA":
            badge = '<span style="background-color:#f3e5f5; color:#6a1b9a; padding:3px 8px; border-radius:4px; font-weight:bold; font-size:11px;">EXTRA IN FABRIC</span>'
            row_bg = "#faf5fc"
        else:
            badge = '<span style="background-color:#fff3e0; color:#ef6c00; padding:3px 8px; border-radius:4px; font-weight:bold; font-size:11px;">MISMATCH</span>'
            row_bg = "#fffaf0"

        diff_tickets_style = "color:#c62828; font-weight:bold;" if row["Diff_Tickets"] != 0 else "color:#2e7d32;"
        diff_rev_style = "color:#c62828; font-weight:bold;" if abs(row["Diff_Revenue"]) > 0.01 else "color:#2e7d32;"

        summary_rows_html.append(f"""
        <tr style="background-color:{row_bg}; border-bottom:1px solid #e0e0e0;">
            <td style="padding:8px; font-size:12px;">{row['Date']}</td>
            <td style="padding:8px; font-size:12px; font-weight:600;">{row['Mall_Name']}</td>
            <td style="padding:8px; font-size:12px; text-align:right;">{row['SkiData_Tickets']:,}</td>
            <td style="padding:8px; font-size:12px; text-align:right;">{row['Fabric_Tickets']:,}</td>
            <td style="padding:8px; font-size:12px; text-align:right; {diff_tickets_style}">{row['Diff_Tickets']:+,}</td>
            <td style="padding:8px; font-size:12px; text-align:right;">{row['SkiData_Revenue']:,.2f}</td>
            <td style="padding:8px; font-size:12px; text-align:right;">{row['Fabric_Revenue']:,.2f}</td>
            <td style="padding:8px; font-size:12px; text-align:right; {diff_rev_style}">{row['Diff_Revenue']:+,.2f}</td>
            <td style="padding:8px; font-size:12px; text-align:center;">{badge}</td>
        </tr>
        """)

    summary_table_body = "\n".join(summary_rows_html)

    # Build Drill-Down Breakdown Section (Below Master Table)
    drilldown_sections_html = []
    if result.total_mismatches > 0:
        drilldown_sections_html.append("""
        <div style="margin-top:30px;">
            <h3 style="color:#1a237e; border-bottom:2px solid #1a237e; padding-bottom:6px; font-family:Arial,sans-serif;">
                🔍 Detailed Inconsistency Breakdowns (Ticket-Level Drilldown)
            </h3>
            <p style="font-size:12px; color:#555; font-family:Arial,sans-serif;">
                The tables below isolate the exact tickets responsible for discrepancies on each mismatched date and mall.
            </p>
        """)

        # Process each mismatched key
        for _, m_row in result.mismatches_df.iterrows():
            key = f"{m_row['Date']} | {m_row['Mall_Name']}"
            fab_missing_df = result.missing_in_fabric.get(key)
            ski_missing_df = result.missing_in_skidata.get(key)

            drilldown_sections_html.append(f"""
            <div style="margin-top:20px; background-color:#ffffff; border:1px solid #dcdcdc; border-radius:6px; padding:12px;">
                <h4 style="margin:0 0 10px 0; color:#b71c1c; font-size:14px; font-family:Arial,sans-serif;">
                    Discrepancy at: <span style="color:#000;">{key}</span> 
                    (Diff: {m_row['Diff_Tickets']:+d} tickets | {m_row['Diff_Revenue']:+,.2f} SAR)
                </h4>
            """)

            # Table A: In SkiData, Missing in Fabric
            if fab_missing_df is not None and not fab_missing_df.empty:
                t_rows = []
                for _, t in fab_missing_df.head(15).iterrows():
                    t_rows.append(f"""
                    <tr style="border-bottom:1px solid #f0f0f0; font-size:11px;">
                        <td style="padding:5px;"><code>{t.get('Ticket_Number', '')}</code></td>
                        <td style="padding:5px;">{t.get('In_Time', '')}</td>
                        <td style="padding:5px;">{t.get('Out_Time', '')}</td>
                        <td style="padding:5px; text-align:right; font-weight:bold;">{float(t.get('Payment', 0.0)):,.2f} SAR</td>
                        <td style="padding:5px;">{t.get('Payment_Device', '')}</td>
                    </tr>
                    """)
                more_note = f"<p style='font-size:11px; color:#777; margin:4px 0 0 0;'>...and {len(fab_missing_df) - 15} more tickets (full list attached in Excel).</p>" if len(fab_missing_df) > 15 else ""
                
                drilldown_sections_html.append(f"""
                <div style="margin-bottom:15px;">
                    <strong style="font-size:12px; color:#c62828;">🔻 Present in SkiData, MISSING in Fabric ({len(fab_missing_df)} tickets):</strong>
                    <table style="width:100%; border-collapse:collapse; margin-top:5px; font-family:Arial,sans-serif;">
                        <thead>
                            <tr style="background-color:#ffebee; color:#b71c1c; font-size:11px; text-align:left;">
                                <th style="padding:5px;">Ticket Sequence</th>
                                <th style="padding:5px;">In Time</th>
                                <th style="padding:5px;">Out Time</th>
                                <th style="padding:5px; text-align:right;">Amount</th>
                                <th style="padding:5px;">Exit Device</th>
                            </tr>
                        </thead>
                        <tbody>{''.join(t_rows)}</tbody>
                    </table>
                    {more_note}
                </div>
                """)

            # Table B: In Fabric, Missing in SkiData
            if ski_missing_df is not None and not ski_missing_df.empty:
                t_rows = []
                for _, t in ski_missing_df.head(15).iterrows():
                    t_rows.append(f"""
                    <tr style="border-bottom:1px solid #f0f0f0; font-size:11px;">
                        <td style="padding:5px;"><code>{t.get('Ticket_Number', '')}</code></td>
                        <td style="padding:5px; text-align:right; font-weight:bold;">{float(t.get('Payment', 0.0)):,.2f} SAR</td>
                    </tr>
                    """)
                drilldown_sections_html.append(f"""
                <div>
                    <strong style="font-size:12px; color:#6a1b9a;">🔺 Present in Fabric, MISSING in SkiData ({len(ski_missing_df)} tickets):</strong>
                    <table style="width:100%; border-collapse:collapse; margin-top:5px; font-family:Arial,sans-serif;">
                        <thead>
                            <tr style="background-color:#f3e5f5; color:#4a148c; font-size:11px; text-align:left;">
                                <th style="padding:5px;">Ticket Sequence</th>
                                <th style="padding:5px; text-align:right;">Amount</th>
                            </tr>
                        </thead>
                        <tbody>{''.join(t_rows)}</tbody>
                    </table>
                </div>
                """)

            drilldown_sections_html.append("</div>")
        drilldown_sections_html.append("</div>")

    drilldown_body = "\n".join(drilldown_sections_html)

    # Master HTML Layout
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; background-color:#f4f6f8; margin:0; padding:20px; }}
            .container {{ max-width: 960px; margin: 0 auto; background: #ffffff; padding: 25px; border-radius: 8px; border: 1px solid #e0e0e0; }}
            .header {{ border-bottom: 2px solid #1a237e; padding-bottom: 15px; margin-bottom: 20px; }}
            .kpi-box {{ display: inline-block; width: 30%; background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 6px; padding: 12px; margin-right: 2%; text-align: center; }}
        </style>
    </head>
    <body style="background-color:#f4f6f8; font-family:Arial,sans-serif; margin:0; padding:20px;">
        <div style="max-width:960px; margin:0 auto; background:#ffffff; padding:25px; border-radius:8px; border:1px solid #e0e0e0;">
            
            <!-- Header -->
            <div style="border-bottom:2px solid #1a237e; padding-bottom:12px; margin-bottom:20px;">
                <div style="float:right; font-size:11px; color:#666; text-align:right;">
                    Environment: <strong>Production (15-Day Rolling)</strong><br>
                    Generated: {result.execution_time.strftime('%d-%b-%Y %I:%M %p AST')}
                </div>
                <h2 style="margin:0; color:#1a237e; font-size:20px;">
                    Makan Parking Operations: SkiData vs. Fabric Audit
                </h2>
                <div style="margin-top:6px; font-size:13px; color:#444;">
                    Reconciliation Window: <strong>{result.start_date}</strong> to <strong>{result.end_date}</strong>
                </div>
            </div>

            <!-- KPI Cards -->
            <div style="margin-bottom:25px;">
                <table style="width:100%; border-collapse:collapse;">
                    <tr>
                        <td style="width:32%; background-color:#f8f9fa; border:1px solid #e0e0e0; border-radius:6px; padding:12px; text-align:center;">
                            <div style="font-size:11px; color:#666; text-transform:uppercase;">Overall Status</div>
                            <div style="font-size:16px; font-weight:bold; color:{status_color}; margin-top:4px;">{status_text}</div>
                        </td>
                        <td style="width:2%;"></td>
                        <td style="width:32%; background-color:#f8f9fa; border:1px solid #e0e0e0; border-radius:6px; padding:12px; text-align:center;">
                            <div style="font-size:11px; color:#666; text-transform:uppercase;">Total Inconsistencies</div>
                            <div style="font-size:16px; font-weight:bold; color:#1a237e; margin-top:4px;">{result.total_mismatches} Malls/Days</div>
                        </td>
                        <td style="width:2%;"></td>
                        <td style="width:32%; background-color:#f8f9fa; border:1px solid #e0e0e0; border-radius:6px; padding:12px; text-align:center;">
                            <div style="font-size:11px; color:#666; text-transform:uppercase;">Net Revenue Variance</div>
                            <div style="font-size:16px; font-weight:bold; color:{'#c62828' if abs(result.total_revenue_diff) > 0.01 else '#2e7d32'}; margin-top:4px;">{result.total_revenue_diff:+,.2f} SAR</div>
                        </td>
                    </tr>
                </table>
            </div>

            <!-- Master Table -->
            <h3 style="color:#1a237e; font-size:15px; margin-bottom:8px;">1. Master Reconciliation Matrix</h3>
            <table style="width:100%; border-collapse:collapse; margin-bottom:20px; font-size:12px;">
                <thead>
                    <tr style="background-color:#1a237e; color:#ffffff; text-align:left;">
                        <th style="padding:8px;">Date</th>
                        <th style="padding:8px;">Mall Name</th>
                        <th style="padding:8px; text-align:right;">SkiData Qty</th>
                        <th style="padding:8px; text-align:right;">Fabric Qty</th>
                        <th style="padding:8px; text-align:right;">Diff (Qty)</th>
                        <th style="padding:8px; text-align:right;">SkiData (SAR)</th>
                        <th style="padding:8px; text-align:right;">Fabric (SAR)</th>
                        <th style="padding:8px; text-align:right;">Diff (SAR)</th>
                        <th style="padding:8px; text-align:center;">Status</th>
                    </tr>
                </thead>
                <tbody>
                    {summary_table_body}
                </tbody>
            </table>

            <!-- Drilldown Breakdowns -->
            {drilldown_body}

            <!-- Footer -->
            <div style="margin-top:30px; border-top:1px solid #e0e0e0; padding-top:10px; font-size:11px; color:#777; text-align:center;">
                Confidential Internal Operational Audit &bull; Sent exclusively to authorized engineering leads: 
                <strong>{', '.join(get_recipients())}</strong> &bull; Generated by Makan Automation System
            </div>
        </div>
    </body>
    </html>
    """


def send_email_via_graph(
    subject: str,
    html_body: str,
    recipients: List[str],
    attachment_path: Optional[str] = None,
    tenant_id: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    sender_email: Optional[str] = None,
) -> bool:
    """Sends email via Microsoft Graph API using OAuth2 client credentials."""
    t_id = tenant_id or os.getenv("TENANT_ID")
    c_id = client_id or os.getenv("CLIENT_ID")
    c_sec = client_secret or os.getenv("CLIENT_SECRET")
    s_email = sender_email or os.getenv("GRAPH_SENDER_EMAIL") or recipients[0]

    if not (t_id and c_id and c_sec):
        logger.warning("Microsoft Graph credentials (TENANT_ID, CLIENT_ID, CLIENT_SECRET) not fully set.")
        return False

    # Get Bearer token
    token_url = f"https://login.microsoftonline.com/{t_id}/oauth2/v2.0/token"
    token_data = {
        "client_id": c_id,
        "client_secret": c_sec,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }
    r = requests.post(token_url, data=token_data, timeout=30)
    if r.status_code != 200:
        logger.error(f"Failed to acquire Microsoft Graph token: {r.text}")
        return False

    token = r.json().get("access_token")

    # Build payload
    to_recipients_payload = [{"emailAddress": {"address": addr.strip()}} for addr in recipients if addr.strip()]
    message_payload = {
        "subject": subject,
        "body": {
            "contentType": "HTML",
            "content": html_body,
        },
        "toRecipients": to_recipients_payload,
        "ccRecipients": [],
        "bccRecipients": [],
    }

    # Add attachment if exists
    if attachment_path and os.path.exists(attachment_path):
        filename = os.path.basename(attachment_path)
        with open(attachment_path, "rb") as f:
            content_bytes = f.read()
        b64_content = base64.b64encode(content_bytes).decode("utf-8")
        message_payload["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename,
                "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "contentBytes": b64_content,
            }
        ]

    # Send mail
    send_url = f"https://graph.microsoft.com/v1.0/users/{s_email}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    res = requests.post(send_url, headers=headers, json={"message": message_payload, "saveToSentItems": "false"}, timeout=45)
    
    if res.status_code in (200, 202):
        logger.info(f"Reconciliation email successfully dispatched via Graph API to: {recipients}")
        return True
    else:
        logger.error(f"Graph API sendMail returned status {res.status_code}: {res.text}")
        return False


def send_email_via_smtp(
    subject: str,
    html_body: str,
    recipients: List[str],
    attachment_path: Optional[str] = None,
) -> bool:
    """Fallback email sender using standard Office 365 SMTP."""
    smtp_server = os.getenv("SMTP_SERVER", "smtp.office365.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")

    if not (smtp_user and smtp_pass):
        logger.warning("SMTP credentials not provided.")
        return False

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    if attachment_path and os.path.exists(attachment_path):
        part = MIMEBase("application", "octet-stream")
        with open(attachment_path, "rb") as f:
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(attachment_path)}"')
        msg.attach(part)

    with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, recipients, msg.as_string())
    
    logger.info(f"Reconciliation email successfully sent via SMTP to: {recipients}")
    return True


def dispatch_reconciliation_report(
    result: ReconciliationResult,
    dry_run: bool = False,
    output_html_path: Optional[str] = None,
) -> bool:
    """Main notifier entry point: builds HTML report and sends via Graph API / SMTP."""
    recipients = get_recipients()
    subject = (
        f"✅ [100% MATCH] SkiData vs. Fabric Reconciliation ({result.start_date} to {result.end_date})"
        if result.total_mismatches == 0
        else f"🚨 [RECON ALERT] {result.total_mismatches} Inconsistencies Found: SkiData vs. Fabric ({result.start_date} to {result.end_date})"
    )

    html_content = build_html_report(result)

    # Save preview HTML locally
    if output_html_path:
        with open(output_html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info(f"HTML Report saved for preview: {output_html_path}")

    if dry_run:
        logger.info("DRY-RUN mode enabled: skipping actual email dispatch.")
        return True

    # Try Graph API first, then SMTP fallback
    sent = send_email_via_graph(
        subject=subject,
        html_body=html_content,
        recipients=recipients,
        attachment_path=result.excel_report_path,
    )
    if not sent:
        logger.info("Attempting fallback email dispatch via Office 365 SMTP...")
        sent = send_email_via_smtp(
            subject=subject,
            html_body=html_content,
            recipients=recipients,
            attachment_path=result.excel_report_path,
        )

    return sent
