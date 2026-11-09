"""ACH return reason codes and short titles.

Ported from moov-io/ach, addenda99.go (returnReasonCodeDictionary) at c7025ec,
Apache License 2.0, copyright The Moov Authors, fetched 2026-11-09. Titles only; the
longer descriptions are not copied. Codes not listed here (such as custom R97) are
accepted by the parser with no title.
"""

from __future__ import annotations

RETURN_REASONS: dict[str, str] = {
    "R01": "Insufficient Funds",
    "R02": "Account Closed",
    "R03": "No Account/Unable to Locate Account",
    "R04": "Invalid Account Number",
    "R05": "Improper Debit to Consumer Account",
    "R06": "Returned per ODFI's Request",
    "R07": "Authorization Revoked by Customer",
    "R08": "Payment Stopped",
    "R09": "Uncollected Funds",
    "R10": (
        "Customer Advises Originator is Not Known to Receiver and/or "
        "Originator is Not Authorized by Receiver to Debit Receiver's Account"
    ),
    "R11": "Customer Advises Entry Not in Accordance with the Terms of the Authorization",
    "R12": "Branch Sold to Another DFI",
    "R13": "RDFI not qualified to participate",
    "R14": "Representative payee deceased or unable to continue in that capacity",
    "R15": "Beneficiary or bank account holder",
    "R16": "Bank account frozen",
    "R17": (
        "File Record Edit Criteria/Entry with Invalid "
        "Account Number Initiated Under Questionable Circumstances"
    ),
    "R18": "Improper effective entry date",
    "R19": "Amount field error",
    "R20": "Non-payment bank account",
    "R21": "Invalid company ID number",
    "R22": "Invalid individual ID number",
    "R23": "Credit entry refused by receiver",
    "R24": "Duplicate entry",
    "R25": "Addenda error",
    "R26": "Mandatory field error",
    "R27": "Trace number error",
    "R28": "Transit routing number check digit error",
    "R29": "Corporate customer advises not authorized",
    "R30": "RDFI not participant in check truncation program",
    "R31": "Permissible return entry (CCD and CTX only)",
    "R32": "RDFI non-settlement",
    "R33": "Return of XCK entry",
    "R34": "Limited participation RDFI",
    "R35": "Return of improper debit entry",
    "R36": "Return of improper credit entry",
    "R37": "Source Document Presented for Payment (Adjustment Entry)",
    "R38": "Stop Payment on Source Document (Adjustment Entry)",
    "R39": "Improper Source Document",
    "R40": "Return of ENR Entry by Federal Government Agency (ENR Only)",
    "R41": "Invalid Transaction Code (ENR only)",
    "R42": "Routing Number/Check Digit Error (ENR Only)",
    "R43": "Invalid DFI Account Number (ENR Only)",
    "R44": "Invalid Individual ID Number/Identification Number (ENR only)",
    "R45": "Invalid Individual Name/Company Name (ENR only)",
    "R46": "Invalid Representative Payee Indicator (ENR Only)",
    "R47": "Duplicate Enrollment (ENR Only)",
    "R50": "State Law Affecting RCK Acceptance",
    "R51": "Item Related to RCK Entry is Ineligible or RCK Entry is Improper",
    "R52": "Stop Payment on Item (Adjustment Entry)",
    "R53": "Item and RCK Entry Presented for Payment (Adjustment Entry)",
    "R61": "Misrouted Return",
    "R62": "Return of Erroneous or Reversing Debt",
    "R67": "Duplicate Return",
    "R68": "Untimely Return",
    "R69": "Field Error(s)",
    "R70": "Permissible Return Entry Not Accepted/Return Not Requested by ODFI",
    "R71": "Misrouted Dishonored Return",
    "R72": "Untimely Dishonored Return",
    "R73": "Timely Original Return",
    "R74": "Corrected Return",
    "R75": "Return Not a Duplicate",
    "R76": "No Errors Found",
    "R77": "Non-Acceptance of R62 Dishonored Return",
    "R80": "IAT Entry Coding Error",
    "R81": "Non-Participant in IAT Program",
    "R82": "Invalid Foreign Receiving DFI Identification",
    "R83": "Foreign Receiving DFI Unable to Settle",
    "R84": "Entry Not Processed by Gateway",
    "R85": "Incorrectly Coded Outbound International Payment",
    "R90": "Entry Returned Due to RDFI's Sanctions Compliance Obligations",
}
