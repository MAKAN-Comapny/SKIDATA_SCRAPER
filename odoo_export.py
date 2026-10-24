"""
Build the Odoo import file from the cleaned Parking Transactions export and upload it
to the "SKIDATA - Odoo" SharePoint folder.

Runs inside the scraper job, straight from the local file, so the Odoo import never
depends on Skidata_Daily_Files (which the Fabric loader empties after loading).
"""
import glob
import os

from odoo_transformer import transform_parking_file
from To_Sharepoint import upload_files

CLEANED_EXPORTS_DIR = "Cleaned-Exports"
ODOO_OUT_DIR = "Odoo-Exports"
ODOO_FOLDER = (
    os.getenv("TARGET_FOLDER_PATH_ODOO")
    or os.getenv("ODOO_SHAREPOINT_FOLDER")
    or "SKIDATA - Odoo"
)


def main():
    sources = glob.glob(os.path.join(CLEANED_EXPORTS_DIR, "*Parking-Transactions.xlsx"))
    if not sources:
        raise SystemExit(f"No Parking-Transactions file in '{CLEANED_EXPORTS_DIR}'.")

    os.makedirs(ODOO_OUT_DIR, exist_ok=True)
    outputs = []
    for src in sources:
        base = os.path.basename(src)
        dest = os.path.join(ODOO_OUT_DIR, base[:-5] + "-Odoo.xlsx")
        before, after = transform_parking_file(src, dest)
        print(f"  {base}: rows {before} -> {after} (Payment != 0)")
        outputs.append(dest)

    print(f"Uploading {len(outputs)} Odoo file(s) to '{ODOO_FOLDER}'...")
    upload_files(outputs, ODOO_FOLDER)


if __name__ == "__main__":
    main()
