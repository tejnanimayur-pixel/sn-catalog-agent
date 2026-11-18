Title: Request a new laptop

As an employee I want to request a new laptop from the Service Portal so that IT can procure and set it up.

Fields:
- Requested for (defaults to the logged-in user)
- Laptop model: Standard 14", Developer 16", MacBook Pro 14" (mandatory)
- Reason: New joiner / Replacement / Upgrade (mandatory)
- If Replacement: Asset tag of current laptop (mandatory) and "Is the current device damaged?" (Yes/No)
- Business justification (multi-line, mandatory when model is MacBook Pro 14")
- Delivery location (reference to locations)

Acceptance Criteria:
1. Item is visible under Hardware category in Service Catalog.
2. Asset tag and damaged question appear only when Reason = Replacement.
3. Business justification is mandatory only for MacBook Pro 14".
4. Requested for defaults to current user.
