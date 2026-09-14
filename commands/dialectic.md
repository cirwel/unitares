---
description: "Request a UNITARES dialectic review for the current agent"
---

Call `dialectic(action="request")` for the current agent. If the user didn't provide a reason, ask for a brief explanation of why they want a review (e.g., "disagreement with verdict", "want peer verification", "high-stakes decision").

Report the result — whether a dialectic session was created and the session ID.

If the user is trying to answer an existing dialectic session rather than create one, use `/dialectic-respond` instead.
