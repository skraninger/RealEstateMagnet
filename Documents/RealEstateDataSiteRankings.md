# Florida Real Estate Data Aggregator: Tech Stack Rankings

This document outlines the top 5 web development stacks for building a data aggregation platform focused on Florida real estate, ranked by both architectural fitness and AI agent compatibility.

---

## 1. Top 5 Architectural Stacks

| Rank  | Stack Name                         | Backend/Data                   | Frontend             | Best Use Case                      |
| :---- | :--------------------------------- | :----------------------------- | :------------------- | :--------------------------------- |
| **1** | **The Data-First Python Stack**    | FastAPI + PostgreSQL (PostGIS) | React / Next.js      | Complex logic & geospatial queries |
| **2** | **The Modern Startup Stack**       | Supabase + Prisma              | Next.js (App Router) | Rapid development & sleek UX       |
| **3** | **The Scalable Pipeline Stack**    | Node.js + Kafka + MongoDB      | React                | High-volume, real-time ingestion   |
| **4** | **The Performance/SEO Stack**      | Go (Golang) + PocketBase       | Astro                | Marketing-focused & high SEO rank  |
| **5** | **The Enterprise Analytics Stack** | .NET 8 + Snowflake             | Angular              | Heavy-duty investment analytics    |

---

## 2. AI Agent Preference Ranking
*Ranked by how effectively AI coding agents (like Cursor or Claude) can generate and maintain the code.*

### **#1: FastAPI + Python (The AI Favorite)**
* **Why:** Python is the native language of LLMs. 
* **Efficiency:** High. Pydantic type hints provide "guide rails" that prevent AI hallucinations.
* **Aggregation:** Agents excel at writing Python-based scrapers and ETL (Extract, Transform, Load) pipelines.

### **#2: Next.js + Prisma + Tailwind**
* **Why:** The most common stack in modern AI training data.
* **Efficiency:** High. Agents are masters of Tailwind CSS and Prisma schemas.
* **Aggregation:** Next.js Server Actions keep data fetching and UI context together for the AI.

### **#3: Astro + Go**
* **Why:** Component-based architecture is easy for AI to replicate.
* **Efficiency:** Medium. While Go is simple, agents are slightly less "creative" with it than Python.

### **#4: Node.js + MongoDB**
* **Why:** Flexible schemas are easy for AI, but Node boilerplate can be "noisy."
* **Efficiency:** Low/Medium. Large projects can lead to the AI losing track of middleware and async chains.

### **#5: .NET 8 + Angular**
* **Why:** Too much boilerplate code.
* **Efficiency:** Low. The verbose nature of these frameworks eats up the AI's "token limit" (context window) quickly.

---

## 3. Summary Comparison

| Feature                | Winner                 | Why?                                                         |
| :--------------------- | :--------------------- | :----------------------------------------------------------- |
| **Data Handling**      | **Python (FastAPI)**   | Superior libraries for standardization and GIS.              |
| **Development Speed**  | **Next.js / Supabase** | Integrated Auth, DB, and UI in one ecosystem.                |
| **Search Visibility**  | **Astro**              | Ships zero-JS by default for instant Florida search rankings. |
| **AI Maintainability** | **Python (FastAPI)**   | Most predictable and concise for AI agents.                  |