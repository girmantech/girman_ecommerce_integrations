import time
from collections import defaultdict
from collections.abc import Iterable

import frappe
from frappe.utils import cint, now

from ecommerce_integrations.controllers.inventory import (
    get_inventory_levels,
    get_inventory_levels_of_group_warehouse,
)
from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.unicommerce.api_client import UnicommerceAPIClient
from ecommerce_integrations.unicommerce.constants import MODULE_NAME, SETTINGS_DOCTYPE
from ecommerce_integrations.unicommerce.utils import create_unicommerce_log


# Maximum number of items sent to Unicommerce in one API request.
MAX_INVENTORY_UPDATE_IN_REQUEST = 1000

# The scheduler runs this job on the "default" queue, which kills jobs after 300 seconds.
# Stop sending new batches after this many seconds; unsent items are picked up in the next run.
MAX_RUN_SECONDS = 240

# Rows updated per query while marking Ecommerce Items as synced.
SYNC_STATUS_CHUNK_SIZE = 500


def update_inventory_on_unicommerce(client=None, force=False):
    """Update ERPNext warehouse-wise inventory to Unicommerce.

    Called by scheduler every 5 minutes. Decides whether to run based on
    configured sync frequency. force=True ignores the set frequency.

    All pending items of a warehouse are sent in batches of MAX_INVENTORY_UPDATE_IN_REQUEST,
    oldest stock change first. A batch that fails (e.g. SKUs unknown to Unicommerce)
    does not stop the next batches from being sent.

    An Ecommerce Item is marked as synced only if Unicommerce accepted it for every
    warehouse where it had a pending change. Otherwise the change stays pending.
    """
    log = None

    try:
        settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)

        if not settings.is_enabled() or not settings.enable_inventory_sync:
            return

        if not force and not need_to_run(
            SETTINGS_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"
        ):
            return

        warehouses = settings.get_erpnext_warehouses()
        wh_to_facility_map = settings.get_erpnext_to_integration_wh_mapping()

        if client is None:
            client = UnicommerceAPIClient()

        log = create_unicommerce_log()

        succeeded: set[str] = set()  # Ecommerce Items accepted by Unicommerce
        unsynced: set[str] = set()  # Ecommerce Items with a change that was NOT accepted somewhere
        inventory_synced_on = now()
        started = time.monotonic()

        total_items_sent = 0
        total_items_synced = 0
        total_items_failed = 0
        total_items_left = 0
        warehouses_processed = 0
        warehouses_failed = 0
        messages = [f"Starting sync for {len(warehouses)} warehouse(s)\n"]

        for idx, warehouse in enumerate(warehouses, 1):
            try:
                messages.append(f"[{idx}/{len(warehouses)}] Warehouse: {warehouse}")

                pending = _get_pending_inventory(warehouse)
                if not pending:
                    messages.append("  -> No items to sync")
                    continue

                facility_code = wh_to_facility_map.get(warehouse)
                if not facility_code:
                    unsynced.update(d.ecom_item for d in pending)
                    messages.append("  -> No facility code mapped for this warehouse")
                    warehouses_failed += 1
                    continue

                messages.append(
                    f"  -> {len(pending)} item(s) pending, sending to facility '{facility_code}' "
                    f"in batches of {MAX_INVENTORY_UPDATE_IN_REQUEST}"
                )

                warehouse_failed = False
                for start in range(0, len(pending), MAX_INVENTORY_UPDATE_IN_REQUEST):
                    batch = pending[start : start + MAX_INVENTORY_UPDATE_IN_REQUEST]

                    if time.monotonic() - started > MAX_RUN_SECONDS:
                        left = pending[start:]
                        unsynced.update(d.ecom_item for d in left)
                        total_items_left += len(left)
                        messages.append(
                            f"  -> Time limit reached, {len(left)} item(s) left for the next run"
                        )
                        break

                    inventory_map = {d.integration_item_code: cint(d.actual_qty) for d in batch}
                    ecom_items_by_sku = defaultdict(list)
                    for d in batch:
                        ecom_items_by_sku[d.integration_item_code].append(d.ecom_item)

                    total_items_sent += len(inventory_map)
                    response, status = client.bulk_inventory_update(
                        facility_code=facility_code,
                        inventory_map=inventory_map,
                    )

                    if not status:
                        left = pending[start:]
                        unsynced.update(d.ecom_item for d in left)
                        total_items_failed += len(inventory_map)
                        total_items_left += len(left) - len(batch)
                        messages.append(
                            f"  -> API returned failure for batch of {len(inventory_map)} item(s), "
                            "remaining items left for the next run"
                        )
                        warehouse_failed = True
                        break

                    batch_synced = 0
                    for sku, status_val in response.items():
                        for ecom_item in ecom_items_by_sku.get(sku, []):
                            (succeeded if status_val else unsynced).add(ecom_item)

                        if status_val:
                            batch_synced += 1

                        # Per-item detail: warehouse, item code, quantity synced
                        messages.append(
                            f"     {warehouse} | {sku} | qty={inventory_map.get(sku)} | "
                            f"{'OK' if status_val else 'FAILED'}"
                        )

                    # SKUs we sent that Unicommerce did not report on are not confirmed
                    for sku in set(ecom_items_by_sku) - set(response):
                        unsynced.update(ecom_items_by_sku[sku])
                        messages.append(
                            f"     {warehouse} | {sku} | qty={inventory_map.get(sku)} | NO RESPONSE"
                        )

                    total_items_synced += batch_synced
                    total_items_failed += len(inventory_map) - batch_synced
                    messages.append(f"  -> {batch_synced}/{len(inventory_map)} items synced in this batch")

                if warehouse_failed:
                    warehouses_failed += 1
                else:
                    warehouses_processed += 1

            except Exception:
                warehouses_failed += 1
                messages.append(f"  -> ERROR: {frappe.get_traceback()}")
                frappe.log_error(
                    title=f"Inventory Sync Failed: {warehouse}",
                    message=frappe.get_traceback(),
                )
                continue

        # Only items accepted for every warehouse with a pending change are marked as synced.
        _update_inventory_sync_status(succeeded - unsynced, inventory_synced_on)

        try:
            frappe.db.set_value(
                SETTINGS_DOCTYPE,
                settings.name,
                "last_inventory_sync",
                now(),
            )
        except Exception:
            frappe.log_error(
                title="Failed to update last_inventory_sync",
                message=frappe.get_traceback(),
            )

        summary = (
            f"\n{'=' * 50}\n"
            f"SUMMARY\n"
            f"{'=' * 50}\n"
            f"Warehouses: {warehouses_processed} success / {warehouses_failed} failed\n"
            f"Items: {total_items_synced} synced / {total_items_failed} failed / "
            f"{total_items_left} left for next run ({total_items_sent} sent)\n"
            f"{'=' * 50}"
        )
        messages.append(summary)

        if log:
            log.message = "\n".join(messages)
            if warehouses_failed:
                log.status = "Error"
            elif total_items_failed or total_items_left:
                log.status = "Warning"
            else:
                log.status = "Success"
            log.save(ignore_permissions=True)

        frappe.db.commit()

    except Exception:
        frappe.log_error(
            title="Unicommerce Inventory Sync - Critical Failure",
            message=frappe.get_traceback(),
        )

        if log:
            log.message = f"CRITICAL ERROR:\n{frappe.get_traceback()}"
            log.status = "Failure"
            log.save(ignore_permissions=True)

        raise


def _get_pending_inventory(warehouse: str) -> list:
    """Items of `warehouse` whose stock changed since they were last synced (oldest change first)."""
    if cint(frappe.db.get_value("Warehouse", warehouse, "is_group")):
        return get_inventory_levels_of_group_warehouse(warehouse=warehouse, integration=MODULE_NAME)

    return get_inventory_levels(warehouses=(warehouse,), integration=MODULE_NAME)


def _update_inventory_sync_status(ecom_items: Iterable[str], timestamp: str) -> None:
    """Mark Ecommerce Items as inventory synced at `timestamp`, in chunks."""
    ecom_items = list(ecom_items)
    EcommerceItem = frappe.qb.DocType("Ecommerce Item")

    for start in range(0, len(ecom_items), SYNC_STATUS_CHUNK_SIZE):
        chunk = ecom_items[start : start + SYNC_STATUS_CHUNK_SIZE]
        try:
            (
                frappe.qb.update(EcommerceItem)
                .set(EcommerceItem.inventory_synced_on, timestamp)
                .where(EcommerceItem.name.isin(chunk))
                .run()
            )
        except Exception:
            frappe.log_error(
                title="Failed to update sync status",
                message=frappe.get_traceback(),
            )
