# Capstone-Project
It is a Simple College Event Register to manage college events in which DS, DBMS and Python fundamentals are implemented.
# EventRegister - College Event Registration (SJCEM)

Flask (Python) + MySQL + HTML/CSS/JS. Supports solo registration, team
registration (any one member registers for the whole group), and an
optional "pay by QR, admin verifies" flow for paid events.

## Run it
1. In MySQL Workbench, run the setup queries you were given (database
   `college_events` with the `admins`, `students`, `events`, `teams`,
   `registrations` and `results` tables).
2. Open `config.py` and put your MySQL password (and host/user if different).
3. `pip install -r requirements.txt`
4. `python app.py`  ->  open http://127.0.0.1:5000

First start creates the admin account automatically: **admin / Admin@123**
(change it in config.py before you present).

## Files
- app.py        routes, business rules (waitlist, clash check, teams, payments, XP, reports)
- ds.py         data structures: Trie, Min-Heap, Queue, Stack, Hash Map, top-N heap, Binary Search
- config.py     DB connection, college email domain, team size limits, upload folders
- templates/    HTML pages (Jinja2)
- static/       CSS + JS (static/uploads/ holds QR codes and payment screenshots, created at startup)

## Team events
The admin turns any event into a team event and picks a team size (2-6).
Any one student registers on behalf of the whole team by entering their
teammates' college emails - teammates need their own EventRegister account
but don't have to do anything themselves. The registering student becomes
the team leader; only the leader can cancel, and cancelling removes the
whole team. Capacity for a team event counts teams, not individual seats.

## Pay by QR
The admin uploads a QR code image and a fee when creating a paid event.
Students scan it, pay, then enter a transaction reference and/or upload a
screenshot. The seat/team slot is held immediately with payment status
"pending"; the admin verifies or rejects the payment from the event's
Registrations page (rejecting removes the registration and frees the slot).
