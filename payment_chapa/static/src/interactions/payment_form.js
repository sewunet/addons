import { _t } from "@web/core/l10n/translation";
import { rpc, RPCError } from "@web/core/network/rpc";
import { patch } from "@web/core/utils/patch";

import { PaymentForm } from "@payment/interactions/payment_form";

/**
 * Chapa direct charge.
 *
 * The member picks a wallet and types a phone number in the inline form, and
 * stays on this page: Odoo asks Chapa to charge the phone, the wallet prompts
 * the member (USSD push, a one-time code, or the bank's own page), and this
 * script polls until Chapa confirms -- then hands over to /payment/status.
 *
 * Only for a provider in direct mode: that is when the inline form exists.
 */
patch(PaymentForm.prototype, {

    async _prepareInlineForm(providerId, providerCode, paymentOptionId, paymentMethodCode, flow) {
        if (providerCode !== "chapa" || !this._chapaDirectForm(providerId)) {
            await super._prepareInlineForm(...arguments);
            return;
        }
        if (flow === "token") {
            return;
        }
        this._setPaymentFlow("direct");
    },

    async _processDirectFlow(providerCode, paymentOptionId, paymentMethodCode, processingValues) {
        if (providerCode !== "chapa") {
            await super._processDirectFlow(...arguments);
            return;
        }
        const form = this._chapaDirectForm(processingValues.provider_id);
        const method = form.querySelector("[name=chapa_method]").value;
        const mobile = form.querySelector("[name=chapa_mobile]").value;
        const status = form.querySelector("[name=chapa_status]");
        const auth = {
            reference: processingValues.reference,
            token: processingValues.chapa_token,
        };
        const say = (text, kind = "info") => {
            status.textContent = text;
            status.className = `alert alert-${kind} mt-3 mb-0`;
        };
        try {
            say(_t("Contacting the wallet…"));
            const started = await rpc("/payment/chapa/charge", { ...auth, type: method, mobile });
            say(started.message);
            if (started.auth_type === "otp") {
                this._chapaAskForCode(form, auth, say);
                return;
            }
            if (started.auth_type === "portal_view" && started.portal_url) {
                window.open(started.portal_url, "_blank", "noopener");
            }
            await this._chapaWait(auth, say);
        } catch (error) {
            if (error instanceof RPCError) {
                say(error.data.message, "danger");
                this._enableButton();
            } else {
                return Promise.reject(error);
            }
        }
    },

    _chapaDirectForm(providerId) {
        return document.getElementById(`o_chapa_direct_${providerId}`);
    },

    _chapaAskForCode(form, auth, say) {
        const block = form.querySelector("[name=chapa_otp]");
        const field = form.querySelector("[name=chapa_otp] input");
        const button = form.querySelector("[name=chapa_otp_submit]");
        block.classList.remove("d-none");
        field.focus();
        button.onclick = async () => {
            button.disabled = true;
            try {
                say(_t("Checking the code…"));
                const result = await rpc("/payment/chapa/authorize", { ...auth, otp: field.value });
                if (result.state === "done") {
                    window.location = "/payment/status";
                    return;
                }
                say(result.message || _t("Code accepted; confirming the payment…"));
                await this._chapaWait(auth, say);
            } catch (error) {
                if (error instanceof RPCError) {
                    say(error.data.message, "danger");
                    button.disabled = false;
                } else {
                    return Promise.reject(error);
                }
            }
        };
    },

    /**
     * Poll until Chapa settles the charge, then go to the status page. If the
     * phone never answers within the window, go there anyway: the status page
     * keeps polling, and the verify cron settles anything approved late.
     */
    async _chapaWait(auth, say) {
        const every = 4000;
        const deadline = Date.now() + 180000;
        while (Date.now() < deadline) {
            await new Promise((resolve) => setTimeout(resolve, every));
            const result = await rpc("/payment/chapa/poll", auth);
            if (["done", "cancel", "error", "authorized"].includes(result.state)) {
                window.location = "/payment/status";
                return;
            }
            if (result.message) {
                say(result.message);
            }
        }
        say(_t("No answer from the phone yet. If you approved it, the payment will be recorded automatically."), "warning");
        window.location = "/payment/status";
    },
});
