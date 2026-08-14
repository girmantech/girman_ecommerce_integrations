"""Sync completed Unicommerce return putaways into ERPNext as Stock Entries.

When a customer/courier return is received and put away in Unicommerce, the
returned stock physically comes back into a facility. This module mirrors that
into ERPNext by creating a `Stock Entry` (Material Receipt) in the facility's
mapped warehouse, per item, routed by inventory type (GOOD / BAD).

Read-only towards Unicommerce: it only calls the `return/search` and
`return/get` endpoints and creates Stock Entries in ERPNext. Nothing is written
back to Unicommerce.
"""

from datetime import datetime
from datetime import timezone as dt_timezone

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, get_system_timezone, now_datetime
from pytz import timezone as pytz_timezone

from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import ecommerce_item
from ecommerce_integrations.unicommerce.api_client import UnicommerceAPIClient, _utc_timeformat
from ecommerce_integrations.unicommerce.constants import MODULE_NAME, SETTINGS_DOCTYPE
from ecommerce_integrations.unicommerce.utils import create_unicommerce_log

RETURN_TYPES = ("CIR", "RTO")

# Terminal/"completed" returnStatus differs by return type:
# CIR (customer) returns end at COMPLETE, RTO (courier) returns at RETURNED.
COMPLETED_STATUS = {"CIR": "COMPLETE", "RTO": "RETURNED"}

# Custom fields on Stock Entry (created via setup_custom_fields)
PUTAWAY_CODE_FIELD = "unicommerce_putaway_code"
REVERSE_PICKUP_FIELD = "unicommerce_reverse_pickup_code"


@frappe.whitelist()
def sync_completed_returns(client: UnicommerceAPIClient = None, force=False):
	"""Scheduled job: create Stock Entries for completed return putaways.

	Gated by:
	  - Unicommerce integration enabled + `sync_returns` toggle
	  - `return_sync_frequency` throttle (unless force)
	  - `returns_sync_start_date` cutoff (so existing/historical putaways are skipped)
	"""
	settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)

	if not settings.is_enabled():
		return

	# `sync_returns` controls the automatic (scheduled) sync. A manual/button run
	# (force=True) is allowed even when automatic sync is turned off.
	if not force:
		if not settings.get("sync_returns"):
			return
		if not need_to_run(SETTINGS_DOCTYPE, "return_sync_frequency", "last_return_sync"):
			return

	if client is None:
		client = UnicommerceAPIClient()

	# Only facilities with "Enable Returns" ticked in the Warehouse Mapping are synced.
	facility_to_wh = {
		wh.unicommerce_facility_code: wh.erpnext_warehouse
		for wh in settings.warehouse_mapping
		if wh.get("enable_returns")
	}
	if not facility_to_wh:
		return

	bad_warehouse = settings.get("returns_bad_inventory_warehouse")
	complete_only = cint(settings.get("only_sync_completed_returns"))

	# Rolling window: only returns UPDATED in the last `return_sync_hours` (like the
	# order sync), so recently-completed putaways are caught without re-scanning all
	# history. Timestamps are sent in UTC, as Unicommerce expects. An optional
	# `returns_sync_start_date` acts as a go-live floor.
	hours = cint(settings.get("return_sync_hours")) or 24
	window_from = add_to_date(now_datetime(), hours=-hours)
	start_date = settings.get("returns_sync_start_date")
	if start_date and get_datetime(start_date) > window_from:
		window_from = get_datetime(start_date)
	# single window (<= a few hours, well within Unicommerce's 30-day limit)
	windows = [(_utc_timeformat(window_from), _utc_timeformat(now_datetime()))]

	stats = {
		"returns_seen": 0,
		"stock_entries_created": 0,
		"skipped_existing": 0,
		"skipped_incomplete": 0,
		"fetch_failed": 0,
		"errors": 0,
	}
	error_snapshots = []
	fac_items = list(facility_to_wh.items())
	log = [f"Starting return putaway sync for {len(fac_items)} facility(s)"]

	try:
		for idx, (facility, warehouse) in enumerate(fac_items, start=1):
			log.append(f"\n[{idx}/{len(fac_items)}] Facility: {facility} -> {warehouse}")
			facility_seen = 0
			facility_failed = False
			for return_type in RETURN_TYPES:
				for created_from, created_to in windows:
					returns = client.search_returns(
						facility, return_type, created_from, created_to, log_error=False
					)
					# None (not []) means the search request itself failed — most often a
					# facility code that is mapped here but does not exist / is not
					# accessible in Unicommerce. Surface it instead of silently syncing 0.
					if returns is None:
						stats["fetch_failed"] += 1
						facility_failed = True
						log.append(
							f"  {return_type} | SEARCH FAILED for facility '{facility}' "
							"(check the facility code exists in Unicommerce)"
						)
						continue
					for row in returns:
						code = row.get("code")
						if not code:
							continue
						stats["returns_seen"] += 1
						facility_seen += 1
						# Each return is isolated: a failure rolls back only its own
						# changes and never stops the rest of the sync.
						try:
							if _process_return(
								code=code,
								return_type=return_type,
								facility=facility,
								facility_warehouse=warehouse,
								bad_warehouse=bad_warehouse,
								complete_only=complete_only,
								client=client,
								stats=stats,
								log=log,
							):
								frappe.db.commit()
						except Exception:
							frappe.db.rollback()
							stats["errors"] += 1
							last_line = frappe.get_traceback(with_context=False).strip().splitlines()
							log.append(f"  {return_type} {code} | ERROR: {(last_line[-1] if last_line else '')[:150]}")
							error_snapshots.append(
								{
									"code": code,
									"return_type": return_type,
									"facility": facility,
									"error": frappe.get_traceback(with_context=False),
								}
							)
			if not facility_seen and not facility_failed:
				log.append("  (no returns found)")

		failed = stats["errors"] + stats["fetch_failed"]
		log.append("\n" + "=" * 50)
		log.append("SUMMARY")
		log.append("=" * 50)
		log.append(
			f"Returns: seen={stats['returns_seen']}, created={stats['stock_entries_created']}, "
			f"skipped_existing={stats['skipped_existing']}, skipped_incomplete={stats['skipped_incomplete']}, "
			f"fetch_failed={stats['fetch_failed']}, errors={stats['errors']}"
		)
		log.append(f"Stock Entries created: {stats['stock_entries_created']}")
		log.append("=" * 50)

		# Log the full line-by-line run whenever there was anything to report.
		if stats["returns_seen"] or failed:
			create_unicommerce_log(
				status="Warning" if failed else "Success",
				method="sync_completed_returns",
				message="\n".join(log),
				request_data={"errors": error_snapshots[:20]} if error_snapshots else None,
			)

	except Exception as e:
		create_unicommerce_log(
			status="Error", method="sync_completed_returns", exception=e, rollback=True
		)
		raise


def _process_return(
	code, return_type, facility, facility_warehouse, bad_warehouse, complete_only, client, stats, log
):
	"""Fetch one return's detail and create a Stock Entry if not already done.

	`code` is the return identifier from search: a reverse pickup code for CIR
	returns, a shipment code for RTO returns. Returns True iff a Stock Entry was
	created (so the caller can commit that one return).
	"""
	# Idempotency: never create a second Stock Entry for the same return.
	if frappe.db.exists("Stock Entry", {REVERSE_PICKUP_FIELD: code, "docstatus": ("<", 2)}):
		stats["skipped_existing"] += 1
		log.append(f"  {return_type} {code} | SKIPPED (already synced)")
		return False

	# RTO (courier) returns are looked up by shipment code; CIR by reverse pickup code.
	if return_type == "RTO":
		detail = client.get_return(facility, shipment_code=code, log_error=False)
	else:
		detail = client.get_return(facility, reverse_pickup_code=code, log_error=False)
	if not detail:
		stats["fetch_failed"] += 1
		log.append(f"  {return_type} {code} | FETCH FAILED")
		return False

	value = detail.get("returnSaleOrderValue") or {}
	putaway_code = value.get("putawayCode")
	# Only returns with a completed putaway move physical stock. A return with no
	# putaway (e.g. "don't expect return") must never create a Stock Entry.
	if not putaway_code:
		stats["skipped_incomplete"] += 1
		log.append(f"  {return_type} {code} | SKIPPED (no putaway)")
		return False
	completed_status = COMPLETED_STATUS.get(return_type)
	if complete_only and completed_status and value.get("returnStatus") != completed_status:
		stats["skipped_incomplete"] += 1
		log.append(
			f"  {return_type} {code} | SKIPPED (status={value.get('returnStatus')}, not {completed_status})"
		)
		return False

	# Only stock that has physically been received should update inventory.
	items = [
		it
		for it in (detail.get("returnSaleOrderItems") or [])
		if it.get("saleOrderItemStatus") == "RECEIVED"
	]
	log.append(
		f"  {return_type} {code} | putaway={putaway_code} | status={value.get('returnStatus')} | received={len(items)}"
	)
	if not items:
		log.append("     (no received items)")
		return False

	se = _build_stock_entry(
		reverse_pickup_code=code,
		putaway_code=putaway_code,
		items=items,
		facility_warehouse=facility_warehouse,
		bad_warehouse=bad_warehouse,
		posting_datetime=_putaway_datetime(value),
		log=log,
	)
	if se:
		stats["stock_entries_created"] += 1
		return True
	return False


def _build_stock_entry(
	reverse_pickup_code, putaway_code, items, facility_warehouse, bad_warehouse, log, posting_datetime=None
):
	"""Build & submit a Material Receipt for the received return items.

	Quantity is implicit in Unicommerce (one row = one unit), so items are
	grouped by (SKU, inventory type) to get the received quantity. Only GOOD
	(sellable) stock is received, into the facility warehouse. BAD inventory is
	intentionally not tracked in ERPNext (see the skip below).
	"""
	# group (sku, inventory_type) -> qty
	groups: dict[tuple, int] = {}
	for it in items:
		key = (it.get("skuCode"), it.get("inventoryType"))
		groups[key] = groups.get(key, 0) + 1

	se_items = []
	problems = []
	for (sku, inventory_type), qty in groups.items():
		# --- Bad inventory is intentionally NOT tracked in ERPNext (business decision) ---
		# Only GOOD (sellable) returned stock is received. To re-enable bad-inventory
		# tracking in future, remove this skip and restore bad-warehouse routing.
		if inventory_type == "BAD_INVENTORY":
			log.append(f"     {sku} | qty={qty} | BAD | SKIPPED (bad inventory not tracked)")
			continue

		item_code = ecommerce_item.get_erpnext_item_code(
			integration=MODULE_NAME, integration_item_code=sku
		)
		if not item_code:
			problems.append(f"{sku}: unmapped SKU")
			log.append(f"     {sku} | qty={qty} | {inventory_type} | ISSUE (unmapped SKU)")
			continue

		item = frappe.get_cached_value(
			"Item", item_code, ["is_stock_item", "disabled", "has_batch_no", "has_serial_no"], as_dict=True
		)
		if not item or not item.is_stock_item or item.disabled:
			problems.append(f"{item_code}: non-stock/disabled")
			log.append(f"     {sku} | qty={qty} | ISSUE (non-stock or disabled item)")
			continue
		# The return API does not supply batch/serial numbers, so such items cannot be
		# received automatically — flag for manual handling instead of failing.
		if item.has_batch_no or item.has_serial_no:
			problems.append(f"{item_code}: batch/serial-tracked")
			log.append(f"     {sku} | qty={qty} | ISSUE (batch/serial-tracked, needs manual receipt)")
			continue
		if not facility_warehouse:
			problems.append(f"{item_code}: no warehouse mapped")
			log.append(f"     {sku} | qty={qty} | ISSUE (no warehouse)")
			continue

		se_items.append(
			{
				"item_code": item_code,
				"qty": qty,
				"t_warehouse": facility_warehouse,
				"basic_rate": _valuation_rate(item_code, facility_warehouse),
				"allow_zero_valuation_rate": 1,
			}
		)
		log.append(f"     {sku} | qty={qty} | GOOD | {facility_warehouse} | OK")

	# All-or-nothing: if any (non-bad) item can't be received cleanly, skip the WHOLE
	# return so nothing is partially synced and permanently locked by idempotency. No
	# Stock Entry is created, so it retries (while still in the sync window) and
	# self-heals once the items are fixed (mapped / batch handling / etc.).
	if problems:
		log.append(
			f"     -> Stock Entry NOT created; return skipped (will retry): {'; '.join(problems)}"
		)
		return None

	if not se_items:
		log.append("     -> nothing to receive (all bad inventory), Stock Entry not created")
		return None

	company = frappe.db.get_value("Warehouse", se_items[0]["t_warehouse"], "company")

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Receipt"
	se.purpose = "Material Receipt"
	se.company = company
	se.set(PUTAWAY_CODE_FIELD, putaway_code)
	se.set(REVERSE_PICKUP_FIELD, reverse_pickup_code)
	se.remarks = f"Unicommerce return putaway {putaway_code or ''} ({reverse_pickup_code})"
	# Post on the actual putaway/receipt date from Unicommerce, not the day the sync
	# happened to run. Falls back to "now" if Unicommerce sent no timestamp.
	if posting_datetime:
		se.set_posting_time = 1
		se.posting_date = posting_datetime.date()
		se.posting_time = posting_datetime.strftime("%H:%M:%S")
	for row in se_items:
		se.append("items", row)

	se.insert(ignore_permissions=True)
	se.submit()
	log.append(f"     -> Stock Entry {se.name} created ({len(se_items)} row(s))")
	return se


def _valuation_rate(item_code, warehouse) -> float:
	"""Book value for the returned stock.

	Prefer the item's current valuation in the target warehouse; fall back to
	the item master valuation / standard rate. `allow_zero_valuation_rate` keeps
	the entry from failing if none is available.
	"""
	rate = frappe.db.get_value(
		"Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate"
	)
	if not rate:
		rate = frappe.db.get_value("Item", item_code, "valuation_rate")
	if not rate:
		rate = frappe.db.get_value("Item", item_code, "standard_rate")
	return flt(rate)


def _putaway_datetime(value):
	"""Actual putaway/receipt datetime (in the site timezone) for the SE posting date.

	Unicommerce sends `inventoryReceivedDate` / `returnCompletedDate` as epoch
	milliseconds in UTC. Returning None lets ERPNext default the posting date to
	"now" when the return carries no completion timestamp.
	"""
	ts = value.get("inventoryReceivedDate") or value.get("returnCompletedDate")
	if not ts:
		return None
	utc_dt = datetime.fromtimestamp(cint(ts) / 1000, tz=dt_timezone.utc)
	return utc_dt.astimezone(pytz_timezone(get_system_timezone())).replace(tzinfo=None)
