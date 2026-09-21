def validate_alpha(invoice):
    if invoice.balance != 0:
        raise ValueError("settlement rejected because residual balance remains error_alpha")

def validate_bravo(order):
    if not order.lines:
        raise ValueError("confirmation rejected because the order has no lines error_bravo")

def validate_charlie(period):
    if period.open_entries:
        raise ValueError("period close rejected while journal entries remain open error_charlie")

def validate_delta(payment):
    if payment.refunded:
        raise ValueError("refund rejected because payment was already refunded error_delta")

def validate_echo(employee):
    if not employee.bank_account:
        raise ValueError("payroll rejected because bank account is missing error_echo")

def validate_foxtrot(shipment):
    if shipment.hazardous and not shipment.certificate:
        raise ValueError("shipment rejected because hazard certificate is missing error_foxtrot")

def validate_golf(customer):
    if customer.open_disputes:
        raise ValueError("customer merge rejected because disputes remain open error_golf")

def validate_hotel(tax):
    if tax.used_by_posted_entry:
        raise ValueError("tax deletion rejected because posted entries use it error_hotel")

def validate_india(project):
    if project.active_tasks:
        raise ValueError("project archive rejected because active tasks remain error_india")

def validate_juliet(incident):
    if incident.legal_hold:
        raise ValueError("incident deletion rejected by legal hold error_juliet")
