# 🍵 IntelliTea – An AI-Inspired Smart Assistant for Tea Business

> Final Year Project | MCA (FYIP) VI Semester | Guru Nanak Dev University, Amritsar | 2026

IntelliTea is an AI-powered chatbot-based system designed to help small tea businesses manage 
customer interactions digitally — replacing manual phone calls with a smart, guided interface 
for placing orders, filing complaints, and getting business information.

---

## 🔗 Live Demo

👉 [Visit IntelliTea Frontend](https://jas023.github.io/IntelliTea/)

---

## 🚀 Features

- 📱 **Customer Interface** — Phone number entry with confirmation dialog
- 🤖 **AI Chatbot** — Guided flow for Order, Complaint, and Know About
- 🛒 **Order Management** — Tea item selection, quantity, address, and order confirmation
- 💳 **Payment Confirmation** — QR code display + UTR/Reference number submission
- 📋 **Complaint System** — Issue type selection with details saved to database
- 🔐 **Admin Dashboard** — Secure login with whitelisted email authorization
- 📦 **Order Tracking** — Admin can view, mark delivered, and delete orders
- 🗄️ **Supabase Database** — Cloud PostgreSQL for leads, orders, complaints, admin tables

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Frontend | HTML, CSS, Tailwind CSS, JavaScript |
| Backend | Python, Flask |
| Database | Supabase (PostgreSQL) |
| Auth | Supabase Auth + authorized_admins whitelist |
| Hosting | GitHub Pages (frontend), Localhost (backend) |

---

## 📁 Project Structure
IntelliTea/
├── docs/               # Frontend (GitHub Pages)
│   ├── index.html      # Landing page
│   ├── phone-page.html # Customer phone entry
│   ├── about-page.html # About / business info
│   ├── recipe.html     # Tea recipes
├── admin/              # Admin portal 
│   ├── dashboard.html  # dashboard page
│   ├── order-managemnet.html #oders page
│   ├── signin.html     # signin page for admin
├── backend/            # Python Flask backend
│   └── app.py          # Main chatbot + API routes
├── requirements.txt    # Python dependencies
└── README.md
---

## ⚙️ How to Run Locally

### Backend (Flask)
```bash
git clone https://github.com/jas023/IntelliTea.git
cd IntelliTea
pip install -r requirements.txt
cd backend
python app.py
```

### Frontend
Open `docs/index.html` in your browser or visit the live GitHub Pages link above.

> ⚠️ Note: The AI chatbot and order/complaint saving features require the Flask backend 
> to be running locally. The frontend pages (landing, about, recipes) work without it.

---

## 🗄️ Database Schema (Supabase)

| Table | Purpose |
|---|---|
| `leads` | Stores customer name + phone from phone entry page |
| `orders` | All customer orders with items, amount, address, payment status |
| `complaints` | Customer complaints with issue type and details |
| `authorized_admins` | Whitelisted admin emails for dashboard access |

---


## 📄 License

This project was developed for academic purposes as a final year BCA project.
