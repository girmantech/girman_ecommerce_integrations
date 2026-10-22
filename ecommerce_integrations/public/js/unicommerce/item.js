frappe.ui.form.on("Item", {
	refresh(frm) {
		if (!frm.is_new() && frappe.user.has_role("System Manager")) {
			// offer the button only while the item has no link yet
			frappe.db
				.get_value(
					"Ecommerce Item",
					{ integration: "unicommerce", erpnext_item_code: frm.doc.name },
					"name",
				)
				.then((r) => {
					if (r.message && r.message.name) {
						return;
					}
					frm.add_custom_button(
						__("Link with Unicommerce"),
						() => link_with_unicommerce(frm),
						__("Unicommerce"),
					);
				});
		}

		if (frm.doc.sync_with_unicommerce) {
			frm.add_custom_button(
				__("Open Unicommerce Item"),
				function () {
					frappe.call({
						method: "ecommerce_integrations.unicommerce.utils.get_unicommerce_document_url",
						args: {
							code: frm.doc.item_code,
							doctype: frm.doc.doctype,
						},
						callback: function (r) {
							if (!r.exc) {
								window.open(r.message, "_blank");
							}
						},
					});
				},
				__("Unicommerce"),
			);
		}
	},
});

function link_with_unicommerce(frm) {
	frappe.prompt(
		[
			{
				fieldname: "sku",
				fieldtype: "Data",
				label: __("Unicommerce SKU"),
				default: frm.doc.item_code,
				reqd: 1,
				description: __(
					"Use the SKU exactly as it appears in Unicommerce. It is the same as the Item Code unless the two systems use different codes.",
				),
			},
		],
		(values) => {
			frappe.call({
				method: "ecommerce_integrations.unicommerce.product.link_item_with_unicommerce",
				args: { item: frm.doc.name, sku: values.sku },
				freeze: true,
				freeze_message: __("Linking with Unicommerce..."),
				callback: (r) => {
					if (!r.exc) {
						frappe.show_alert({
							message: __("Linked with Unicommerce SKU {0}", [r.message.sku]),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
		__("Link with Unicommerce"),
		__("Link"),
	);
}
