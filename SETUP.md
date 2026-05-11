# Investment Tracker — Setup Guide

## What's included
```
investment-tracker/
├── backend/
│   ├── app.py            ← Flask API (all endpoints)
│   ├── requirements.txt  ← Python dependencies
│   ├── .env.example      ← Copy to .env and fill in
│   ├── Procfile          ← For Railway/Render deploy
│   └── railway.toml      ← Railway config
└── frontend/
    └── index.html        ← Complete frontend app
```

---

## Step 1 — Create MongoDB Atlas (5 min, free)

1. Go to https://cloud.mongodb.com and create a free account
2. Create a new **free M0 cluster** (any region)
3. Under **Database Access** → Add a user with a password
4. Under **Network Access** → Add IP `0.0.0.0/0` (allow all, fine for dev)
5. Click **Connect** → **Drivers** → copy the connection string:
   ```
   mongodb+srv://USERNAME:PASSWORD@cluster0.XXXXX.mongodb.net/?retryWrites=true&w=majority
   ```

---

## Step 2 — Run the backend locally

```bash
cd investment-tracker/backend

# 1. Copy env file
cp .env.example .env

# 2. Edit .env — paste your MongoDB URI
#    MONGO_URI=mongodb+srv://USERNAME:PASSWORD@cluster0.XXXXX.mongodb.net/...

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the server
python app.py
```

You should see:
```
* Running on http://0.0.0.0:5000
```

Test it: open http://localhost:5000/health in your browser.
You should see: `{"status": "ok", "db": "investment_tracker"}`

---

## Step 3 — Open the frontend

Just open `frontend/index.html` in your browser (double-click it).

- The API URL box defaults to `http://localhost:5000`
- Click **Connect** — the dot turns green if your Flask server is running
- Start logging entries!

---

## Step 4 — Deploy to Railway (so it works from anywhere)

### Backend on Railway

1. Install Railway CLI:
   ```bash
   npm install -g @railway/cli
   ```

2. Login and deploy:
   ```bash
   cd investment-tracker/backend
   railway login
   railway init        # create new project
   railway up          # deploy
   ```

3. Add environment variables in Railway dashboard:
   - `MONGO_URI` = your Atlas connection string
   - `DB_NAME` = investment_tracker
   - `PORT` = 5000

4. Get your public URL from Railway dashboard, e.g.:
   ```
   https://investment-tracker-production.up.railway.app
   ```

### Frontend on Vercel (optional — makes it a real hosted app)

1. Install Vercel CLI:
   ```bash
   npm install -g vercel
   ```

2. Deploy:
   ```bash
   cd investment-tracker/frontend
   vercel
   ```

3. Update the API URL in the app's config bar to your Railway URL.

---

## API Endpoints Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /health | Check if API is running |
| GET | /entries | Get all entries (supports ?platform=X&stock=Y) |
| POST | /entries | Add a new entry |
| PUT | /entries/:id | Update an entry |
| DELETE | /entries/:id | Delete an entry |
| GET | /summary | Totals, platform breakdown, stock breakdown |
| GET | /platforms | List of distinct platforms |
| GET | /stocks | List of distinct stocks |

---

## Adding authentication later (optional)

When you're ready to add user accounts:

1. Add Google OAuth with `flask-dance` or `authlib`
2. Add `user_id` to every entry document
3. Filter all queries by `user_id` from the JWT token
4. Use `flask-jwt-extended` for token management

---

## Troubleshooting

**Dot stays red / can't connect:**
- Make sure `python app.py` is running in a terminal
- Check the API URL has no trailing slash
- Check your .env file has the correct MONGO_URI

**MongoDB connection error:**
- Make sure your IP is whitelisted in Atlas Network Access
- Double-check username/password in the URI (special chars must be URL-encoded)

**CORS error in browser:**
- Flask-CORS is already installed and enabled for all origins
- If deploying, make sure flask-cors is in requirements.txt
