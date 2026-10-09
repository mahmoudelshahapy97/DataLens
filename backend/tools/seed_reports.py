#!/usr/bin/env python3
"""Fifty parameterised reports, six or seven per seeded workspace.

    python tools/seed_reports.py --tenant all --password ...
    python tools/seed_reports.py --tenant chinook --password ... --clean

Every report is a `Dashboard` with `parameters`, posted over the app's own API so it
goes through `verify_dashboard` on the way in and `render_dashboard` on the way out.
Nothing is written behind the application's back, which also means a report that would
not render is reported here rather than discovered by whoever opens it.

## Two decisions worth knowing about

**Date defaults are explicit, not relative.** The obvious default for a report period is
`last_30_days`, and it would render six of these eight workspaces completely empty:
Northwind's orders stop in May 2008, `employees` stops in 2002, `booking`'s extract
covers 2015-2017. A default that produces a blank page teaches people the feature is
broken. So each workspace's period defaults to a range its data actually covers, and the
relative windows remain available in the control for the two warehouses where they mean
something.

**Every column here was checked against the live warehouse.** Not against the repo's
seed SQL, which disagrees with what is loaded in at least four places -- `healthcare`
appointment statuses, `chinook` album nullability, ecommerce payment statuses and the
number of `visittype` values. Where a rule and the data conflicted, the data won.

The chart specs lean on `color_by`, `sort_by`, `descending` and `limit`, which were
declared on `ChartSpec` from the beginning and read by nothing until now.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# Parameter helpers
# ----------------------------------------------------------------------


def period(start: str, end: str, label: str = "Period") -> Dict[str, Any]:
    """A date range with an explicit default. Fills {{ period_start/_end }}."""
    return {
        "name": "period",
        "type": "date_range",
        "label": label,
        "default": f"{start}..{end}",
    }


def top(default: int = 10, label: str = "How many") -> Dict[str, Any]:
    return {
        "name": "top",
        "type": "integer",
        "label": label,
        "default": default,
        "minimum": 1,
        "maximum": 100,
    }


def choice(name: str, options: List[str], default: str, label: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "type": "enum",
        "label": label or name.replace("_", " ").title(),
        "options": options,
        "default": default,
    }


def metric(title: str, sql: str, x: int, width: int = 3) -> Dict[str, Any]:
    return {
        "kind": "metric", "title": title,
        "query": {"source": "sql", "sql": sql},
        "grid": {"x": x, "y": 0, "width": width, "height": 3},
    }


def chart(title: str, sql: str, spec: Dict[str, Any], *, y: int, x: int = 0,
          width: int = 12, height: int = 6) -> Dict[str, Any]:
    return {
        "kind": "chart", "title": title,
        "query": {"source": "sql", "sql": sql},
        "chart": spec,
        "grid": {"x": x, "y": y, "width": width, "height": height},
    }


def table(title: str, sql: str, *, y: int, x: int = 0, width: int = 12,
          height: int = 5) -> Dict[str, Any]:
    return {
        "kind": "table", "title": title,
        "query": {"source": "sql", "sql": sql},
        "grid": {"x": x, "y": y, "width": width, "height": height},
    }


def note(text: str, *, y: int = 0) -> Dict[str, Any]:
    return {"kind": "text", "text": text, "grid": {"x": 0, "y": y, "width": 12, "height": 2}}


# ----------------------------------------------------------------------
# chinook -- digital music store. invoice_date spans 2021-01-01 .. 2025-12-22.
# ----------------------------------------------------------------------

CHINOOK_PERIOD = ("2021-01-01", "2025-12-31")

CHINOOK: List[Dict[str, Any]] = [
    {
        "title": "Revenue overview",
        "description": "Headline figures and the monthly trend, over a period you choose.",
        "parameters": [period(*CHINOOK_PERIOD)],
        "tiles": [
            metric("Revenue", """SELECT ROUND(SUM(total)::numeric, 2) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Invoices", """SELECT COUNT(*) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Average invoice", """SELECT ROUND(AVG(total)::numeric, 2) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Countries billed", """SELECT COUNT(DISTINCT billing_country) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Revenue by month", """SELECT TO_CHAR(DATE_TRUNC('month', invoice_date), 'YYYY-MM') AS month,
                         ROUND(SUM(total)::numeric, 2) AS revenue
                  FROM invoices
                  WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["revenue"], "y_label": "Revenue (USD)"},
                  y=3),
        ],
    },
    {
        "title": "Revenue by country",
        "description": "Where invoices are billed. The tail is gathered rather than dropped.",
        "parameters": [period(*CHINOOK_PERIOD), top(12)],
        "tiles": [
            chart("Revenue by billing country", """SELECT billing_country AS country,
                         ROUND(SUM(total)::numeric, 2) AS revenue
                  FROM invoices
                  WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "country", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 12}, y=0, width=7),
            chart("Share of revenue", """SELECT billing_country AS country,
                         ROUND(SUM(total)::numeric, 2) AS revenue
                  FROM invoices
                  WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "country", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 8}, y=0, x=7, width=5),
            table("Every country", """SELECT billing_country AS country, COUNT(*) AS invoices,
                         ROUND(SUM(total)::numeric, 2) AS revenue,
                         ROUND(AVG(total)::numeric, 2) AS average_invoice
                  FROM invoices
                  WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Revenue by country over time",
        "description": "One line per country. The legend is the country column, not a guess.",
        "parameters": [period(*CHINOOK_PERIOD), top(6, "Countries to plot")],
        "tiles": [
            chart("Monthly revenue by country", """SELECT TO_CHAR(DATE_TRUNC('month', invoice_date), 'YYYY-MM') AS month,
                         billing_country AS country,
                         ROUND(SUM(total)::numeric, 2) AS revenue
                  FROM invoices
                  WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["revenue"], "color_by": "country",
                   "limit": 6, "y_label": "Revenue (USD)"}, y=0, height=7),
        ],
    },
    {
        "title": "Cities by revenue",
        "description": "Billing city: where the money was billed, not where the customer lives.",
        "parameters": [period(*CHINOOK_PERIOD), top(15)],
        "tiles": [
            chart("Top cities", """SELECT billing_city AS city,
                         ROUND(SUM(total)::numeric, 2) AS revenue
                  FROM invoices
                  WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "city", "y": ["revenue"], "sort_by": "revenue",
                   "descending": True, "limit": 15}, y=0, height=6),
            table("City detail", """SELECT billing_city AS city, billing_country AS country,
                         COUNT(*) AS invoices, ROUND(SUM(total)::numeric, 2) AS revenue
                  FROM invoices
                  WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Catalogue profile",
        "description": "Length and size come from the model already converted to minutes and MB.",
        "parameters": [top(20)],
        "tiles": [
            metric("Tracks", "SELECT COUNT(*) FROM tracks", 0, 4),
            metric("Average minutes", "SELECT ROUND(AVG(minutes)::numeric, 2) FROM tracks", 4, 4),
            metric("Named composers",
                   "SELECT COUNT(DISTINCT composer) FROM tracks WHERE composer IS NOT NULL", 8, 4),
            chart("Longest tracks", """SELECT name AS track, ROUND(minutes::numeric, 1) AS minutes
                  FROM tracks""",
                  {"type": "bar", "x": "track", "y": ["minutes"], "sort_by": "minutes",
                   "descending": True, "limit": 15}, y=3, height=6),
            table("Most prolific composers", """SELECT composer, COUNT(*) AS tracks,
                         ROUND(AVG(minutes)::numeric, 1) AS average_minutes
                  FROM tracks WHERE composer IS NOT NULL
                  GROUP BY 1 ORDER BY tracks DESC LIMIT {{ top }}""", y=9),
        ],
    },
    {
        "title": "Customer base",
        "description": "Where customers are, which can differ from where their invoices are billed.",
        "parameters": [top(15)],
        "tiles": [
            chart("Customers by country", """SELECT country, COUNT(*) AS customers
                  FROM customers GROUP BY 1""",
                  {"type": "bar", "x": "country", "y": ["customers"],
                   "sort_by": "customers", "descending": True, "limit": 15}, y=0, width=7),
            chart("Customer share", """SELECT country, COUNT(*) AS customers
                  FROM customers GROUP BY 1""",
                  {"type": "pie", "x": "country", "y": ["customers"],
                   "sort_by": "customers", "descending": True, "limit": 8},
                  y=0, x=7, width=5),
            table("Customers", """SELECT full_name AS customer, city, country, company
                  FROM customers ORDER BY country, city LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "The team",
        "description": "Eight employees, of whom only three ever carry customers.",
        "parameters": [top(10)],
        "tiles": [
            note("Revenue per rep is deliberately absent. Reaching it needs the invoice-to-"
                 "customer join, and the semantic layer exposes no key for it -- writing that "
                 "join by hand returns a cross product that runs and reports the whole "
                 "company's revenue against every rep."),
            table("Employees", """SELECT full_name AS employee, title, city, country,
                         hire_date::date AS hired
                  FROM employees ORDER BY hire_date LIMIT {{ top }}""", y=2, height=5),
            chart("Hires by year", """SELECT EXTRACT(YEAR FROM hire_date)::int AS year,
                         COUNT(*) AS hires
                  FROM employees GROUP BY 1 ORDER BY 1""",
                  {"type": "bar", "x": "year", "y": ["hires"]}, y=7, height=5),
        ],
    },
]


# ----------------------------------------------------------------------
# northwind -- orderdate spans 2006-07-04 .. 2008-05-06.
# Columns are one word: unitprice, qty, orderdate, shippeddate, mgrid.
# ----------------------------------------------------------------------

NW_PERIOD = ("2006-07-01", "2008-05-31")

NORTHWIND: List[Dict[str, Any]] = [
    {
        "title": "Sales overview",
        "description": "Line revenue is unitprice * qty * (1 - discount).",
        "parameters": [period(*NW_PERIOD)],
        "tiles": [
            metric("Revenue", """SELECT ROUND(SUM(d.unitprice * d.qty * (1 - d.discount))::numeric, 2)
                   FROM northwind.orderdetail d
                   JOIN northwind.salesorder o ON o.orderid = d.orderid
                   WHERE o.orderdate BETWEEN {{ period_start }} AND {{ period_end }}""", 0, 4),
            metric("Orders", """SELECT COUNT(*) FROM northwind.salesorder
                   WHERE orderdate BETWEEN {{ period_start }} AND {{ period_end }}""", 4, 4),
            metric("Freight", """SELECT ROUND(SUM(freight)::numeric, 2) FROM northwind.salesorder
                   WHERE orderdate BETWEEN {{ period_start }} AND {{ period_end }}""", 8, 4),
            chart("Revenue by month", """SELECT TO_CHAR(DATE_TRUNC('month', o.orderdate), 'YYYY-MM') AS month,
                         ROUND(SUM(d.unitprice * d.qty * (1 - d.discount))::numeric, 2) AS revenue
                  FROM northwind.orderdetail d
                  JOIN northwind.salesorder o ON o.orderid = d.orderid
                  WHERE o.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "area", "x": "month", "y": ["revenue"]}, y=3),
        ],
    },
    {
        "title": "Revenue by category",
        "description": "Category names are real; product and company names are anonymised.",
        "parameters": [period(*NW_PERIOD)],
        "tiles": [
            chart("Revenue by category", """SELECT c.categoryname AS category,
                         ROUND(SUM(d.unitprice * d.qty * (1 - d.discount))::numeric, 2) AS revenue
                  FROM northwind.orderdetail d
                  JOIN northwind.salesorder o ON o.orderid = d.orderid
                  JOIN northwind.product p ON p.productid = d.productid
                  JOIN northwind.category c ON c.categoryid = p.categoryid
                  WHERE o.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "category", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True}, y=0, width=7),
            chart("Category share", """SELECT c.categoryname AS category,
                         ROUND(SUM(d.unitprice * d.qty * (1 - d.discount))::numeric, 2) AS revenue
                  FROM northwind.orderdetail d
                  JOIN northwind.salesorder o ON o.orderid = d.orderid
                  JOIN northwind.product p ON p.productid = d.productid
                  JOIN northwind.category c ON c.categoryid = p.categoryid
                  WHERE o.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "category", "y": ["revenue"]}, y=0, x=7, width=5),
        ],
    },
    {
        "title": "Shipping performance",
        "description": "A null shippeddate is not shipped at all, which is not a delay of zero.",
        "parameters": [period(*NW_PERIOD)],
        "tiles": [
            metric("Not yet shipped", """SELECT COUNT(*) FROM northwind.salesorder
                   WHERE shippeddate IS NULL
                     AND orderdate BETWEEN {{ period_start }} AND {{ period_end }}""", 0, 4),
            metric("Shipped late", """SELECT COUNT(*) FROM northwind.salesorder
                   WHERE shippeddate > requireddate
                     AND orderdate BETWEEN {{ period_start }} AND {{ period_end }}""", 4, 4),
            metric("Median days to ship", """SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
                       ORDER BY (shippeddate::date - orderdate::date))::numeric, 1)
                   FROM northwind.salesorder
                   WHERE shippeddate IS NOT NULL
                     AND orderdate BETWEEN {{ period_start }} AND {{ period_end }}""", 8, 4),
            table("Orders that shipped after their required date", """SELECT o.orderid, o.orderdate::date AS ordered,
                         o.requireddate::date AS required, o.shippeddate::date AS shipped,
                         (o.shippeddate::date - o.requireddate::date) AS days_late,
                         o.shipcountry AS country
                  FROM northwind.salesorder o
                  WHERE o.shippeddate > o.requireddate
                    AND o.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  ORDER BY days_late DESC LIMIT 25""", y=3, height=6),
        ],
    },
    {
        "title": "Revenue by country over time",
        "description": "One line per shipping country; the legend is the country column.",
        "parameters": [period(*NW_PERIOD), top(6, "Countries to plot")],
        "tiles": [
            chart("Monthly revenue by country", """SELECT TO_CHAR(DATE_TRUNC('month', o.orderdate), 'YYYY-MM') AS month,
                         o.shipcountry AS country,
                         ROUND(SUM(d.unitprice * d.qty * (1 - d.discount))::numeric, 2) AS revenue
                  FROM northwind.orderdetail d
                  JOIN northwind.salesorder o ON o.orderid = d.orderid
                  WHERE o.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["revenue"], "color_by": "country",
                   "limit": 6}, y=0, height=7),
        ],
    },
    {
        "title": "Supplier dependence",
        "description": "How much of the catalogue, and the revenue, rests on each supplier.",
        "parameters": [period(*NW_PERIOD), top(12)],
        "tiles": [
            table("Suppliers by revenue", """SELECT s.companyname AS supplier, s.country,
                         COUNT(DISTINCT p.productid) AS products,
                         ROUND(SUM(d.unitprice * d.qty * (1 - d.discount))::numeric, 2) AS revenue
                  FROM northwind.orderdetail d
                  JOIN northwind.salesorder o ON o.orderid = d.orderid
                  JOIN northwind.product p ON p.productid = d.productid
                  JOIN northwind.supplier s ON s.supplierid = p.supplierid
                  WHERE o.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY revenue DESC LIMIT {{ top }}""", y=0, height=7),
        ],
    },
    {
        "title": "Discount exposure",
        "description": "Discount is a fraction between 0.00 and 0.25, not a percentage.",
        "parameters": [period(*NW_PERIOD)],
        "tiles": [
            chart("Discount given by category", """SELECT c.categoryname AS category,
                         ROUND(SUM(d.unitprice * d.qty * d.discount)::numeric, 2) AS discount_given
                  FROM northwind.orderdetail d
                  JOIN northwind.salesorder o ON o.orderid = d.orderid
                  JOIN northwind.product p ON p.productid = d.productid
                  JOIN northwind.category c ON c.categoryid = p.categoryid
                  WHERE o.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "category", "y": ["discount_given"],
                   "sort_by": "discount_given", "descending": True}, y=0, height=6),
        ],
    },
]

# ----------------------------------------------------------------------
# world -- no dates anywhere, so the parameters are an enum and a count.
# Join column is `countrycode`; `isofficial` is a 'T'/'F' character.
# ----------------------------------------------------------------------

CONTINENTS = ["Africa", "Asia", "Europe", "North America", "Oceania", "South America"]

WORLD: List[Dict[str, Any]] = [
    {
        "title": "Continent overview",
        "description": "Population, countries and cities for one continent.",
        "parameters": [choice("continent", CONTINENTS, "Europe")],
        "tiles": [
            metric("Countries", """SELECT COUNT(*) FROM world.country
                   WHERE continent = {{ continent }}""", 0, 4),
            metric("Population", """SELECT SUM(population) FROM world.country
                   WHERE continent = {{ continent }}""", 4, 4),
            metric("Cities on file", """SELECT COUNT(*) FROM world.city c
                   JOIN world.country co ON co.code = c.countrycode
                   WHERE co.continent = {{ continent }}""", 8, 4),
            chart("Largest countries by population", """SELECT name AS country, population
                  FROM world.country WHERE continent = {{ continent }} AND population > 0""",
                  {"type": "bar", "x": "country", "y": ["population"],
                   "sort_by": "population", "descending": True, "limit": 15}, y=3),
        ],
    },
    {
        "title": "Largest cities",
        "description": "City populations are the city proper, not the metropolitan area.",
        "parameters": [choice("continent", CONTINENTS, "Asia"), top(20)],
        "tiles": [
            table("Cities by population", """SELECT c.name AS city, co.name AS country,
                         c.district, c.population
                  FROM world.city c
                  JOIN world.country co ON co.code = c.countrycode
                  WHERE co.continent = {{ continent }}
                  ORDER BY c.population DESC LIMIT {{ top }}""", y=0, height=7),
        ],
    },
    {
        "title": "Wealth against life expectancy",
        "description": "GNP is in millions of USD; population can be zero, so the divide is guarded.",
        "parameters": [choice("continent", CONTINENTS, "Africa")],
        "tiles": [
            chart("GNP per capita against life expectancy", """SELECT name AS country,
                         ROUND((gnp * 1000000 / NULLIF(population, 0))::numeric, 0) AS gnp_per_capita,
                         lifeexpectancy
                  FROM world.country
                  WHERE continent = {{ continent }} AND gnp IS NOT NULL
                    AND lifeexpectancy IS NOT NULL AND population > 0""",
                  {"type": "scatter", "x": "gnp_per_capita", "y": ["lifeexpectancy"],
                   "x_label": "GNP per capita (USD)", "y_label": "Life expectancy (years)"},
                  y=0, height=7),
        ],
    },
    {
        "title": "Languages spoken",
        "description": "isofficial is 'T' or 'F'. Percentages within a country need not sum to 100.",
        "parameters": [choice("continent", CONTINENTS, "Europe"), top(15)],
        "tiles": [
            chart("Most spoken languages", """SELECT cl.language,
                         ROUND(SUM(co.population * cl.percentage / 100.0)::numeric, 0) AS speakers
                  FROM world.countrylanguage cl
                  JOIN world.country co ON co.code = cl.countrycode
                  WHERE co.continent = {{ continent }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "language", "y": ["speakers"],
                   "sort_by": "speakers", "descending": True, "limit": 15}, y=0, width=7),
            table("Official languages", """SELECT co.name AS country, cl.language, cl.percentage
                  FROM world.countrylanguage cl
                  JOIN world.country co ON co.code = cl.countrycode
                  WHERE co.continent = {{ continent }} AND cl.isofficial = 'T'
                  ORDER BY cl.percentage DESC LIMIT {{ top }}""", y=0, x=7, width=5, height=6),
        ],
    },
    {
        "title": "Government and independence",
        "description": "indepyear is null for 47 countries, which is a fact rather than a gap.",
        "parameters": [choice("continent", CONTINENTS, "South America")],
        "tiles": [
            chart("Government forms", """SELECT governmentform, COUNT(*) AS countries
                  FROM world.country WHERE continent = {{ continent }} GROUP BY 1""",
                  {"type": "pie", "x": "governmentform", "y": ["countries"]}, y=0, width=6),
            table("Most recently independent", """SELECT name AS country, indepyear, population
                  FROM world.country
                  WHERE continent = {{ continent }} AND indepyear IS NOT NULL
                  ORDER BY indepyear DESC LIMIT 20""", y=0, x=6, width=6, height=6),
        ],
    },
    {
        "title": "Population density",
        "description": "Surface area is in square kilometres.",
        "parameters": [choice("continent", CONTINENTS, "Asia"), top(15)],
        "tiles": [
            table("Densest countries", """SELECT name AS country, population,
                         ROUND(surfacearea::numeric, 0) AS area_km2,
                         ROUND((population / NULLIF(surfacearea, 0))::numeric, 1) AS per_km2
                  FROM world.country
                  WHERE continent = {{ continent }} AND population > 0 AND surfacearea > 0
                  ORDER BY per_km2 DESC LIMIT {{ top }}""", y=0, height=7),
        ],
    },
]

# ----------------------------------------------------------------------
# healthcare -- appointment status is Completed/Scheduled/No-Show/Cancelled,
# billing is Paid/Pending, labreferrals is Ordered/Completed. Department is
# reached through medstaffid -> medicalstaff.departmentid.
# ----------------------------------------------------------------------

HC_PERIOD = ("2024-01-01", "2025-12-31")
VISIT_TYPES = ["Checkup", "Consultation", "Follow-up", "Urgency"]

HEALTHCARE: List[Dict[str, Any]] = [
    {
        "title": "Clinic overview",
        "description": "Attendance means status = Completed. A no-show is not a visit.",
        "parameters": [period(*HC_PERIOD)],
        "tiles": [
            metric("Appointments", """SELECT COUNT(*) FROM healthcare.appointments
                   WHERE appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Completed", """SELECT COUNT(*) FROM healthcare.appointments
                   WHERE status = 'Completed'
                     AND appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("No-shows", """SELECT COUNT(*) FROM healthcare.appointments
                   WHERE status = 'No-Show'
                     AND appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Patients seen", """SELECT COUNT(DISTINCT patientid) FROM healthcare.appointments
                   WHERE status = 'Completed'
                     AND appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Appointments by status, by month", """SELECT TO_CHAR(DATE_TRUNC('month', appointmentdate), 'YYYY-MM') AS month,
                         status, COUNT(*) AS appointments
                  FROM healthcare.appointments
                  WHERE appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY 1""",
                  {"type": "bar", "x": "month", "y": ["appointments"], "color_by": "status",
                   "stacked": True}, y=3, height=6),
        ],
    },
    {
        "title": "Department workload",
        "description": "Department comes through the clinician, not the appointment.",
        "parameters": [period(*HC_PERIOD)],
        "tiles": [
            chart("Appointments by department", """SELECT d.departmentname AS department, COUNT(*) AS appointments
                  FROM healthcare.appointments a
                  JOIN healthcare.medicalstaff m ON m.medstaffid = a.medstaffid
                  JOIN healthcare.departments d ON d.departmentid = m.departmentid
                  WHERE a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "department", "y": ["appointments"],
                   "sort_by": "appointments", "descending": True}, y=0, height=6),
            table("Clinicians by volume", """SELECT m.firstname || ' ' || m.lastname AS clinician,
                         m.role, d.departmentname AS department, COUNT(*) AS appointments
                  FROM healthcare.appointments a
                  JOIN healthcare.medicalstaff m ON m.medstaffid = a.medstaffid
                  JOIN healthcare.departments d ON d.departmentid = m.departmentid
                  WHERE a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2, 3 ORDER BY appointments DESC LIMIT 20""", y=6),
        ],
    },
    {
        "title": "Billing and collection",
        "description": "Billed is every row; collected is the Paid ones. The gap is the receivable.",
        "parameters": [period(*HC_PERIOD)],
        "tiles": [
            metric("Billed", """SELECT ROUND(SUM(b.amount)::numeric, 2)
                   FROM healthcare.billing b JOIN healthcare.appointments a
                     ON a.appointmentid = b.appointmentid
                   WHERE a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 0, 4),
            metric("Collected", """SELECT ROUND(SUM(b.amount)::numeric, 2)
                   FROM healthcare.billing b JOIN healthcare.appointments a
                     ON a.appointmentid = b.appointmentid
                   WHERE b.status = 'Paid'
                     AND a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 4, 4),
            metric("Outstanding", """SELECT ROUND(SUM(b.amount)::numeric, 2)
                   FROM healthcare.billing b JOIN healthcare.appointments a
                     ON a.appointmentid = b.appointmentid
                   WHERE b.status = 'Pending'
                     AND a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 8, 4),
            chart("Paid against pending", """SELECT b.status, ROUND(SUM(b.amount)::numeric, 2) AS amount
                  FROM healthcare.billing b JOIN healthcare.appointments a
                    ON a.appointmentid = b.appointmentid
                  WHERE a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "status", "y": ["amount"]}, y=3, width=5, height=5),
            chart("Payment methods", """SELECT b.paymentmethod, COUNT(*) AS bills
                  FROM healthcare.billing b JOIN healthcare.appointments a
                    ON a.appointmentid = b.appointmentid
                  WHERE a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "paymentmethod", "y": ["bills"]},
                  y=3, x=5, width=7, height=5),
        ],
    },
    {
        "title": "Visit types",
        "description": "One of Checkup, Consultation, Follow-up or Urgency. Nothing else is recorded.",
        "parameters": [period(*HC_PERIOD), choice("visittype", VISIT_TYPES, "Consultation")],
        "tiles": [
            chart("Chosen visit type over time", """SELECT TO_CHAR(DATE_TRUNC('month', appointmentdate), 'YYYY-MM') AS month,
                         COUNT(*) AS appointments
                  FROM healthcare.appointments
                  WHERE visittype = {{ visittype }}
                    AND appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["appointments"]}, y=0, height=6),
            chart("All visit types", """SELECT visittype, COUNT(*) AS appointments
                  FROM healthcare.appointments
                  WHERE appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "visittype", "y": ["appointments"]}, y=6, width=6, height=5),
        ],
    },
    {
        "title": "Prescribing",
        "description": "Prescriptions hang off the appointment, so the appointment is the join.",
        "parameters": [period(*HC_PERIOD), top(15)],
        "tiles": [
            chart("Most prescribed", """SELECT p.medicationname AS medication, COUNT(*) AS times
                  FROM healthcare.prescriptions p
                  JOIN healthcare.appointments a ON a.appointmentid = p.appointmentid
                  WHERE a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "medication", "y": ["times"],
                   "sort_by": "times", "descending": True, "limit": 15}, y=0, height=6),
            table("Prescriptions per patient", """SELECT pa.firstname || ' ' || pa.lastname AS patient,
                         COUNT(*) AS prescriptions
                  FROM healthcare.prescriptions p
                  JOIN healthcare.appointments a ON a.appointmentid = p.appointmentid
                  JOIN healthcare.patients pa ON pa.patientid = a.patientid
                  WHERE a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY prescriptions DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Lab referrals outstanding",
        "description": "A referral is Ordered until it is Completed, and has no result until then.",
        "parameters": [period(*HC_PERIOD)],
        "tiles": [
            metric("Ordered", """SELECT COUNT(*) FROM healthcare.labreferrals l
                   JOIN healthcare.appointments a ON a.appointmentid = l.appointmentid
                   WHERE l.status = 'Ordered'
                     AND a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 0, 6),
            metric("Completed", """SELECT COUNT(*) FROM healthcare.labreferrals l
                   JOIN healthcare.appointments a ON a.appointmentid = l.appointmentid
                   WHERE l.status = 'Completed'
                     AND a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 6, 6),
            table("Still waiting on a result", """SELECT l.testtype AS test, a.appointmentdate::date AS ordered_on,
                         d.departmentname AS department
                  FROM healthcare.labreferrals l
                  JOIN healthcare.appointments a ON a.appointmentid = l.appointmentid
                  JOIN healthcare.medicalstaff m ON m.medstaffid = a.medstaffid
                  JOIN healthcare.departments d ON d.departmentid = m.departmentid
                  WHERE l.status = 'Ordered'
                    AND a.appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  ORDER BY a.appointmentdate LIMIT 30""", y=3, height=7),
        ],
    },
]

# ----------------------------------------------------------------------
# chinook, continued -- the catalogue side. Genre and artist revenue both
# reach a sale only through invoice_lines -> tracks -> albums -> artists;
# there is no direct link, which is why these joins look long.
# ----------------------------------------------------------------------

CHINOOK_MORE: List[Dict[str, Any]] = [
    {
        "title": "Genres and formats",
        "description": "What sells, by genre and by file format.",
        "parameters": [period(*CHINOOK_PERIOD), top(12, "Genres to plot")],
        "tiles": [
            chart("Revenue by genre", """SELECT g.name AS genre,
                         ROUND(SUM(il.line_revenue)::numeric, 2) AS revenue
                  FROM invoice_lines il
                  JOIN invoices i ON i.invoice_id = il.invoice_id
                  JOIN tracks t ON t.track_id = il.track_id
                  JOIN genres g ON g.genre_id = t.genre_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "genre", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 12,
                   "y_label": "Revenue (USD)"}, y=0, width=7),
            chart("Share by format", """SELECT m.name AS format, COUNT(*) AS tracks_sold
                  FROM invoice_lines il
                  JOIN invoices i ON i.invoice_id = il.invoice_id
                  JOIN tracks t ON t.track_id = il.track_id
                  JOIN media_types m ON m.media_type_id = t.media_type_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "format", "y": ["tracks_sold"],
                   "sort_by": "tracks_sold", "descending": True}, y=0, x=7, width=5),
            table("Genre detail", """SELECT g.name AS genre,
                         COUNT(*) AS tracks_sold,
                         ROUND(SUM(il.line_revenue)::numeric, 2) AS revenue,
                         ROUND(AVG(il.unit_price)::numeric, 2) AS average_price
                  FROM invoice_lines il
                  JOIN invoices i ON i.invoice_id = il.invoice_id
                  JOIN tracks t ON t.track_id = il.track_id
                  JOIN genres g ON g.genre_id = t.genre_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Artists and albums that earn",
        "description": "Revenue attributed back through album to artist.",
        "parameters": [period(*CHINOOK_PERIOD), top(15)],
        "tiles": [
            chart("Top artists by revenue", """SELECT ar.name AS artist,
                         ROUND(SUM(il.line_revenue)::numeric, 2) AS revenue
                  FROM invoice_lines il
                  JOIN invoices i ON i.invoice_id = il.invoice_id
                  JOIN tracks t ON t.track_id = il.track_id
                  JOIN albums al ON al.album_id = t.album_id
                  JOIN artists ar ON ar.artist_id = al.artist_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "artist", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 15}, y=0),
            table("Albums by revenue", """SELECT al.title AS album, ar.name AS artist,
                         COUNT(*) AS tracks_sold,
                         ROUND(SUM(il.line_revenue)::numeric, 2) AS revenue
                  FROM invoice_lines il
                  JOIN invoices i ON i.invoice_id = il.invoice_id
                  JOIN tracks t ON t.track_id = il.track_id
                  JOIN albums al ON al.album_id = t.album_id
                  JOIN artists ar ON ar.artist_id = al.artist_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Playlists and catalogue reach",
        "description": "How much of the catalogue is on a playlist, and how much has never sold.",
        "parameters": [top(15, "Playlists to list")],
        "tiles": [
            metric("Tracks", "SELECT COUNT(*) FROM tracks", 0),
            metric("Tracks ever sold",
                   "SELECT COUNT(DISTINCT track_id) FROM invoice_lines", 3),
            metric("Playlists", "SELECT COUNT(*) FROM playlists", 6),
            metric("Playlist entries", "SELECT COUNT(*) FROM playlist_tracks", 9),
            chart("Tracks per playlist", """SELECT p.name AS playlist, COUNT(*) AS tracks
                  FROM playlist_tracks pt
                  JOIN playlists p ON p.playlist_id = pt.playlist_id
                  GROUP BY 1""",
                  {"type": "bar", "x": "playlist", "y": ["tracks"],
                   "sort_by": "tracks", "descending": True, "limit": 12}, y=3),
            table("Longest tracks never sold", """SELECT t.name AS track,
                         ROUND(t.minutes::numeric, 1) AS minutes,
                         t.unit_price AS price
                  FROM tracks t
                  LEFT JOIN invoice_lines il ON il.track_id = t.track_id
                  WHERE il.track_id IS NULL
                  GROUP BY 1, 2, 3 ORDER BY minutes DESC LIMIT {{ top }}""", y=9),
        ],
    },
]


# ----------------------------------------------------------------------
# demo -- the same music database as chinook, so the reports deliberately
# take the operational view (customers, support, catalogue housekeeping)
# rather than repeating chinook's revenue-and-geography set.
# ----------------------------------------------------------------------

DEMO_PERIOD = ("2021-01-01", "2025-12-31")

DEMO: List[Dict[str, Any]] = [
    {
        "title": "Store overview",
        "description": "The headline numbers, and the monthly shape of the business.",
        "parameters": [period(*DEMO_PERIOD)],
        "tiles": [
            metric("Revenue", """SELECT ROUND(SUM(total)::numeric, 2) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Invoices", """SELECT COUNT(*) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Customers billed", """SELECT COUNT(DISTINCT customer_id) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Average invoice", """SELECT ROUND(AVG(total)::numeric, 2) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Invoices and revenue by month", """SELECT TO_CHAR(DATE_TRUNC('month', invoice_date), 'YYYY-MM') AS month,
                         COUNT(*) AS invoices,
                         ROUND(SUM(total)::numeric, 2) AS revenue
                  FROM invoices
                  WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["revenue", "invoices"]}, y=3),
        ],
    },
    {
        "title": "Who buys the most",
        "description": "Customers ranked by spend, with where they are.",
        "parameters": [period(*DEMO_PERIOD), top(15)],
        "tiles": [
            chart("Top customers by spend", """SELECT c.full_name AS customer,
                         ROUND(SUM(i.total)::numeric, 2) AS spend
                  FROM invoices i JOIN customers c ON c.customer_id = i.customer_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "customer", "y": ["spend"],
                   "sort_by": "spend", "descending": True, "limit": 15}, y=0),
            table("Customer detail", """SELECT c.full_name AS customer, c.city, c.country,
                         COUNT(*) AS invoices,
                         ROUND(SUM(i.total)::numeric, 2) AS spend,
                         ROUND(AVG(i.total)::numeric, 2) AS average_invoice
                  FROM invoices i JOIN customers c ON c.customer_id = i.customer_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2, 3 ORDER BY spend DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Support rep performance",
        "description": "Each rep's book of customers, and what it is worth.",
        "parameters": [period(*DEMO_PERIOD)],
        "tiles": [
            chart("Revenue by support rep", """SELECT e.full_name AS rep,
                         ROUND(SUM(i.total)::numeric, 2) AS revenue
                  FROM invoices i
                  JOIN customers c ON c.customer_id = i.customer_id
                  JOIN employees e ON e.employee_id = c.support_rep_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "rep", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True}, y=0, width=7),
            chart("Customers per rep", """SELECT e.full_name AS rep, COUNT(*) AS customers
                  FROM customers c JOIN employees e ON e.employee_id = c.support_rep_id
                  GROUP BY 1""",
                  {"type": "pie", "x": "rep", "y": ["customers"]}, y=0, x=7, width=5),
            table("Rep detail", """SELECT e.full_name AS rep, e.title,
                         COUNT(DISTINCT c.customer_id) AS customers,
                         COUNT(i.invoice_id) AS invoices,
                         ROUND(SUM(i.total)::numeric, 2) AS revenue
                  FROM employees e
                  JOIN customers c ON c.support_rep_id = e.employee_id
                  LEFT JOIN invoices i ON i.customer_id = c.customer_id
                       AND i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY revenue DESC NULLS LAST""", y=6),
        ],
    },
    {
        "title": "Genre popularity",
        "description": "Units and revenue by genre, over a period you choose.",
        "parameters": [period(*DEMO_PERIOD), top(12)],
        "tiles": [
            chart("Units sold by genre", """SELECT g.name AS genre, SUM(il.quantity) AS units
                  FROM invoice_lines il
                  JOIN invoices i ON i.invoice_id = il.invoice_id
                  JOIN tracks t ON t.track_id = il.track_id
                  JOIN genres g ON g.genre_id = t.genre_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "genre", "y": ["units"],
                   "sort_by": "units", "descending": True, "limit": 12}, y=0),
            table("Genre detail", """SELECT g.name AS genre, SUM(il.quantity) AS units,
                         ROUND(SUM(il.line_revenue)::numeric, 2) AS revenue
                  FROM invoice_lines il
                  JOIN invoices i ON i.invoice_id = il.invoice_id
                  JOIN tracks t ON t.track_id = il.track_id
                  JOIN genres g ON g.genre_id = t.genre_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Format mix",
        "description": "Which file formats the catalogue holds, and which ones sell.",
        "parameters": [],
        "tiles": [
            chart("Catalogue by format", """SELECT m.name AS format, COUNT(*) AS tracks
                  FROM tracks t JOIN media_types m ON m.media_type_id = t.media_type_id
                  GROUP BY 1""",
                  {"type": "pie", "x": "format", "y": ["tracks"],
                   "sort_by": "tracks", "descending": True}, y=0, width=6),
            chart("Revenue by format", """SELECT m.name AS format,
                         ROUND(SUM(il.line_revenue)::numeric, 2) AS revenue
                  FROM invoice_lines il
                  JOIN tracks t ON t.track_id = il.track_id
                  JOIN media_types m ON m.media_type_id = t.media_type_id
                  GROUP BY 1""",
                  {"type": "bar", "x": "format", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True}, y=0, x=6, width=6),
            table("Format detail", """SELECT m.name AS format, COUNT(DISTINCT t.track_id) AS tracks,
                         ROUND(AVG(t.minutes)::numeric, 1) AS average_minutes,
                         ROUND(AVG(t.megabytes)::numeric, 1) AS average_megabytes
                  FROM tracks t JOIN media_types m ON m.media_type_id = t.media_type_id
                  GROUP BY 1 ORDER BY tracks DESC""", y=6),
        ],
    },
    {
        "title": "Track length and price",
        "description": "How the catalogue is distributed by running time.",
        "parameters": [],
        "tiles": [
            metric("Tracks", "SELECT COUNT(*) FROM tracks", 0),
            metric("Average minutes",
                   "SELECT ROUND(AVG(minutes)::numeric, 1) FROM tracks", 3),
            metric("Longest track",
                   "SELECT ROUND(MAX(minutes)::numeric, 1) FROM tracks", 6),
            metric("Average price",
                   "SELECT ROUND(AVG(unit_price)::numeric, 2) FROM tracks", 9),
            chart("Tracks by length", """SELECT CASE
                           WHEN minutes < 2 THEN 'Under 2 min'
                           WHEN minutes < 4 THEN '2 to 4 min'
                           WHEN minutes < 6 THEN '4 to 6 min'
                           WHEN minutes < 10 THEN '6 to 10 min'
                           ELSE 'Over 10 min' END AS length_band,
                         COUNT(*) AS tracks
                  FROM tracks GROUP BY 1 ORDER BY MIN(minutes)""",
                  {"type": "bar", "x": "length_band", "y": ["tracks"]}, y=3),
            table("Longest tracks", """SELECT name AS track, composer,
                         ROUND(minutes::numeric, 1) AS minutes,
                         ROUND(megabytes::numeric, 1) AS megabytes
                  FROM tracks ORDER BY minutes DESC LIMIT 15""", y=9),
        ],
    },
    {
        "title": "Album catalogue",
        "description": "Which artists have the deepest catalogue.",
        "parameters": [top(15)],
        "tiles": [
            chart("Albums per artist", """SELECT ar.name AS artist, COUNT(*) AS albums
                  FROM albums al JOIN artists ar ON ar.artist_id = al.artist_id
                  GROUP BY 1""",
                  {"type": "bar", "x": "artist", "y": ["albums"],
                   "sort_by": "albums", "descending": True, "limit": 15}, y=0),
            table("Albums by track count", """SELECT al.title AS album, ar.name AS artist,
                         COUNT(*) AS tracks,
                         ROUND(SUM(t.minutes)::numeric, 1) AS total_minutes
                  FROM tracks t
                  JOIN albums al ON al.album_id = t.album_id
                  JOIN artists ar ON ar.artist_id = al.artist_id
                  GROUP BY 1, 2 ORDER BY tracks DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Invoice sizes",
        "description": "Whether the business is many small baskets or a few large ones.",
        "parameters": [period(*DEMO_PERIOD)],
        "tiles": [
            metric("Smallest invoice", """SELECT ROUND(MIN(total)::numeric, 2) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Median-ish (average)", """SELECT ROUND(AVG(total)::numeric, 2) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Largest invoice", """SELECT ROUND(MAX(total)::numeric, 2) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Invoices", """SELECT COUNT(*) FROM invoices
                   WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Invoices by size band", """SELECT CASE
                           WHEN total < 2 THEN 'Under 2'
                           WHEN total < 6 THEN '2 to 6'
                           WHEN total < 10 THEN '6 to 10'
                           WHEN total < 15 THEN '10 to 15'
                           ELSE '15 and over' END AS size_band,
                         COUNT(*) AS invoices
                  FROM invoices
                  WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY MIN(total)""",
                  {"type": "bar", "x": "size_band", "y": ["invoices"]}, y=3),
            table("Largest invoices", """SELECT i.invoice_id, i.invoice_date::date AS invoice_date,
                         c.full_name AS customer, i.billing_country AS country, i.total
                  FROM invoices i JOIN customers c ON c.customer_id = i.customer_id
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  ORDER BY i.total DESC LIMIT 15""", y=9),
        ],
    },
    {
        "title": "Country reach",
        "description": "Where the customers are, against where the money is.",
        "parameters": [period(*DEMO_PERIOD)],
        "tiles": [
            chart("Customers by country", """SELECT country, COUNT(*) AS customers
                  FROM customers GROUP BY 1""",
                  {"type": "bar", "x": "country", "y": ["customers"],
                   "sort_by": "customers", "descending": True, "limit": 15}, y=0, width=6),
            chart("Revenue by country", """SELECT billing_country AS country,
                         ROUND(SUM(total)::numeric, 2) AS revenue
                  FROM invoices
                  WHERE invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "country", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 15}, y=0, x=6, width=6),
            table("Country detail", """SELECT i.billing_country AS country,
                         COUNT(DISTINCT i.customer_id) AS customers,
                         COUNT(*) AS invoices,
                         ROUND(SUM(i.total)::numeric, 2) AS revenue,
                         ROUND(AVG(i.total)::numeric, 2) AS average_invoice
                  FROM invoices i
                  WHERE i.invoice_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY revenue DESC""", y=6),
        ],
    },
    {
        "title": "Quiet catalogue",
        "description": "Tracks that have never sold. Housekeeping, not a sales report.",
        "parameters": [top(20, "Rows to list")],
        "tiles": [
            metric("Tracks", "SELECT COUNT(*) FROM tracks", 0),
            metric("Never sold", """SELECT COUNT(*) FROM tracks t
                   LEFT JOIN invoice_lines il ON il.track_id = t.track_id
                   WHERE il.track_id IS NULL""", 3),
            metric("Genres with no sale", """SELECT COUNT(*) FROM genres g
                   WHERE NOT EXISTS (
                     SELECT 1 FROM tracks t
                     JOIN invoice_lines il ON il.track_id = t.track_id
                     WHERE t.genre_id = g.genre_id)""", 6),
            metric("Albums with no sale", """SELECT COUNT(*) FROM albums a
                   WHERE NOT EXISTS (
                     SELECT 1 FROM tracks t
                     JOIN invoice_lines il ON il.track_id = t.track_id
                     WHERE t.album_id = a.album_id)""", 9),
            chart("Unsold tracks by genre", """SELECT g.name AS genre, COUNT(*) AS unsold
                  FROM tracks t
                  JOIN genres g ON g.genre_id = t.genre_id
                  LEFT JOIN invoice_lines il ON il.track_id = t.track_id
                  WHERE il.track_id IS NULL
                  GROUP BY 1""",
                  {"type": "bar", "x": "genre", "y": ["unsold"],
                   "sort_by": "unsold", "descending": True, "limit": 12}, y=3),
            table("Unsold, most expensive first", """SELECT t.name AS track, g.name AS genre,
                         t.unit_price AS price, ROUND(t.minutes::numeric, 1) AS minutes
                  FROM tracks t
                  LEFT JOIN genres g ON g.genre_id = t.genre_id
                  LEFT JOIN invoice_lines il ON il.track_id = t.track_id
                  WHERE il.track_id IS NULL
                  ORDER BY t.unit_price DESC, t.minutes DESC LIMIT {{ top }}""", y=9),
        ],
    },
]


# ----------------------------------------------------------------------
# northwind, continued. Line revenue is unitprice * qty * (1 - discount);
# `discontinued` is text ('0'/'1'), not a boolean, which is why the
# comparisons below are quoted.
# ----------------------------------------------------------------------

NORTHWIND_MORE: List[Dict[str, Any]] = [
    {
        "title": "Employee sales league",
        "description": "Who sold what, over a period you choose.",
        "parameters": [period(*NW_PERIOD), top(12)],
        "tiles": [
            chart("Revenue by employee", """SELECT e.firstname || ' ' || e.lastname AS employee,
                         ROUND(SUM(od.unitprice * od.qty * (1 - od.discount))::numeric, 2) AS revenue
                  FROM salesorder so
                  JOIN orderdetail od ON od.orderid = so.orderid
                  JOIN employee e ON e.empid = so.empid
                  WHERE so.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "employee", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 12}, y=0, width=7),
            chart("Orders per employee", """SELECT e.firstname || ' ' || e.lastname AS employee,
                         COUNT(DISTINCT so.orderid) AS orders
                  FROM salesorder so JOIN employee e ON e.empid = so.empid
                  WHERE so.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "employee", "y": ["orders"],
                   "sort_by": "orders", "descending": True}, y=0, x=7, width=5),
            table("Employee detail", """SELECT e.firstname || ' ' || e.lastname AS employee,
                         e.title, e.city, e.country,
                         COUNT(DISTINCT so.orderid) AS orders,
                         ROUND(SUM(od.unitprice * od.qty * (1 - od.discount))::numeric, 2) AS revenue
                  FROM salesorder so
                  JOIN orderdetail od ON od.orderid = so.orderid
                  JOIN employee e ON e.empid = so.empid
                  WHERE so.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2, 3, 4 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Freight and shippers",
        "description": "What carriage costs, and which carrier carries it.",
        "parameters": [period(*NW_PERIOD)],
        "tiles": [
            metric("Total freight", """SELECT ROUND(SUM(freight)::numeric, 2) FROM salesorder
                   WHERE orderdate BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Average freight", """SELECT ROUND(AVG(freight)::numeric, 2) FROM salesorder
                   WHERE orderdate BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Orders", """SELECT COUNT(*) FROM salesorder
                   WHERE orderdate BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Not yet shipped", """SELECT COUNT(*) FROM salesorder
                   WHERE shippeddate IS NULL
                     AND orderdate BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Monthly freight by shipper", """SELECT TO_CHAR(DATE_TRUNC('month', so.orderdate), 'YYYY-MM') AS month,
                         sh.companyname AS shipper,
                         ROUND(SUM(so.freight)::numeric, 2) AS freight
                  FROM salesorder so JOIN shipper sh ON sh.shipperid = so.shipperid
                  WHERE so.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["freight"], "color_by": "shipper"}, y=3),
            table("Shipper detail", """SELECT sh.companyname AS shipper,
                         COUNT(*) AS orders,
                         ROUND(SUM(so.freight)::numeric, 2) AS freight,
                         ROUND(AVG(so.freight)::numeric, 2) AS average_freight,
                         COUNT(*) FILTER (WHERE so.shippeddate IS NULL) AS still_open
                  FROM salesorder so JOIN shipper sh ON sh.shipperid = so.shipperid
                  WHERE so.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY freight DESC""", y=9),
        ],
    },
    {
        "title": "Stock and reorder",
        "description": "What is running out, and what has been discontinued anyway.",
        "parameters": [top(20, "Rows to list")],
        "tiles": [
            metric("Products", "SELECT COUNT(*) FROM product", 0),
            metric("Discontinued", "SELECT COUNT(*) FROM product WHERE discontinued = '1'", 3),
            metric("At or below reorder level", """SELECT COUNT(*) FROM product
                   WHERE unitsinstock <= reorderlevel AND reorderlevel > 0""", 6),
            metric("Units on order", "SELECT SUM(unitsonorder) FROM product", 9),
            chart("Stock by category", """SELECT c.categoryname AS category,
                         SUM(p.unitsinstock) AS in_stock,
                         SUM(p.unitsonorder) AS on_order
                  FROM product p JOIN category c ON c.categoryid = p.categoryid
                  GROUP BY 1""",
                  {"type": "bar", "x": "category", "y": ["in_stock", "on_order"],
                   "stacked": True, "sort_by": "in_stock", "descending": True}, y=3),
            table("Needs reordering", """SELECT p.productname AS product, c.categoryname AS category,
                         p.unitsinstock AS in_stock, p.reorderlevel AS reorder_level,
                         p.unitsonorder AS on_order, p.discontinued
                  FROM product p JOIN category c ON c.categoryid = p.categoryid
                  WHERE p.unitsinstock <= p.reorderlevel AND p.reorderlevel > 0
                  ORDER BY (p.reorderlevel - p.unitsinstock) DESC LIMIT {{ top }}""", y=9),
        ],
    },
    {
        "title": "Customer concentration",
        "description": "How much of the book sits with the largest few accounts.",
        "parameters": [period(*NW_PERIOD), top(15)],
        "tiles": [
            chart("Revenue by customer", """SELECT cu.companyname AS customer,
                         ROUND(SUM(od.unitprice * od.qty * (1 - od.discount))::numeric, 2) AS revenue
                  FROM salesorder so
                  JOIN orderdetail od ON od.orderid = so.orderid
                  JOIN customer cu ON cu.custid = so.custid
                  WHERE so.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "customer", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 15}, y=0, width=7),
            chart("Share of revenue", """SELECT cu.companyname AS customer,
                         ROUND(SUM(od.unitprice * od.qty * (1 - od.discount))::numeric, 2) AS revenue
                  FROM salesorder so
                  JOIN orderdetail od ON od.orderid = so.orderid
                  JOIN customer cu ON cu.custid = so.custid
                  WHERE so.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "customer", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 8}, y=0, x=7, width=5),
            table("Customer detail", """SELECT cu.companyname AS customer, cu.city, cu.country,
                         COUNT(DISTINCT so.orderid) AS orders,
                         ROUND(SUM(od.unitprice * od.qty * (1 - od.discount))::numeric, 2) AS revenue
                  FROM salesorder so
                  JOIN orderdetail od ON od.orderid = so.orderid
                  JOIN customer cu ON cu.custid = so.custid
                  WHERE so.orderdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2, 3 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
]


# ----------------------------------------------------------------------
# world, continued. 239 countries, 232 of which name a capital, so the
# capital join has to be an outer one or seven countries vanish silently.
# countrylanguage.isofficial is 'T'/'F' text, not a boolean.
# ----------------------------------------------------------------------

WORLD_MORE: List[Dict[str, Any]] = [
    {
        "title": "Capital cities",
        "description": "Capitals and how much of the country lives in them.",
        "parameters": [choice("continent", CONTINENTS, "Europe"), top(15)],
        "tiles": [
            chart("Largest capitals", """SELECT ci.name AS capital, ci.population
                  FROM country co JOIN city ci ON ci.id = co.capital
                  WHERE co.continent = {{ continent }}""",
                  {"type": "bar", "x": "capital", "y": ["population"],
                   "sort_by": "population", "descending": True, "limit": 15}, y=0),
            table("Capital against country", """SELECT co.name AS country, ci.name AS capital,
                         ci.population AS capital_population,
                         co.population AS country_population,
                         CASE WHEN co.population > 0
                              THEN ROUND(100.0 * ci.population / co.population, 1)
                              ELSE NULL END AS percent_in_capital
                  FROM country co LEFT JOIN city ci ON ci.id = co.capital
                  WHERE co.continent = {{ continent }}
                  ORDER BY percent_in_capital DESC NULLS LAST LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Official languages",
        "description": "Which languages carry official status, and where they do not.",
        "parameters": [top(15)],
        "tiles": [
            metric("Language rows", "SELECT COUNT(*) FROM countrylanguage", 0),
            metric("Official", "SELECT COUNT(*) FROM countrylanguage WHERE isofficial = 'T'", 3),
            metric("Distinct languages",
                   "SELECT COUNT(DISTINCT language) FROM countrylanguage", 6),
            metric("Countries covered",
                   "SELECT COUNT(DISTINCT countrycode) FROM countrylanguage", 9),
            chart("Most widely official", """SELECT language, COUNT(*) AS countries
                  FROM countrylanguage WHERE isofficial = 'T' GROUP BY 1""",
                  {"type": "bar", "x": "language", "y": ["countries"],
                   "sort_by": "countries", "descending": True, "limit": 15}, y=3),
            table("Largest speaker shares", """SELECT cl.language, co.name AS country,
                         cl.percentage AS percent_of_population,
                         CASE WHEN cl.isofficial = 'T' THEN 'official' ELSE 'not official' END AS status
                  FROM countrylanguage cl JOIN country co ON co.code = cl.countrycode
                  ORDER BY cl.percentage DESC LIMIT {{ top }}""", y=9),
        ],
    },
    {
        "title": "Surface area against population",
        "description": "How crowded a country is, plotted rather than ranked.",
        "parameters": [choice("continent", CONTINENTS, "Asia")],
        "tiles": [
            chart("Area against population", """SELECT name AS country, surfacearea AS surface_area,
                         population
                  FROM country
                  WHERE continent = {{ continent }} AND population > 0""",
                  {"type": "scatter", "x": "surface_area", "y": ["population"],
                   "x_label": "Surface area (km2)", "y_label": "Population"}, y=0),
            table("Densest first", """SELECT name AS country, population, surfacearea AS surface_area,
                         CASE WHEN surfacearea > 0
                              THEN ROUND((population / surfacearea)::numeric, 1)
                              ELSE NULL END AS people_per_km2
                  FROM country
                  WHERE continent = {{ continent }}
                  ORDER BY people_per_km2 DESC NULLS LAST LIMIT 20""", y=6),
        ],
    },
    {
        "title": "Regions within continents",
        "description": "The sub-regions the dataset uses, and how they compare.",
        "parameters": [choice("continent", CONTINENTS, "Africa")],
        "tiles": [
            chart("Countries per region", """SELECT region, COUNT(*) AS countries
                  FROM country WHERE continent = {{ continent }} GROUP BY 1""",
                  {"type": "bar", "x": "region", "y": ["countries"],
                   "sort_by": "countries", "descending": True}, y=0, width=6),
            chart("Population per region", """SELECT region, SUM(population) AS population
                  FROM country WHERE continent = {{ continent }} GROUP BY 1""",
                  {"type": "pie", "x": "region", "y": ["population"],
                   "sort_by": "population", "descending": True}, y=0, x=6, width=6),
            table("Region detail", """SELECT region, COUNT(*) AS countries,
                         SUM(population) AS population,
                         ROUND(AVG(lifeexpectancy)::numeric, 1) AS average_life_expectancy,
                         ROUND(SUM(gnp)::numeric, 1) AS total_gnp
                  FROM country WHERE continent = {{ continent }}
                  GROUP BY 1 ORDER BY population DESC""", y=6),
        ],
    },
]


# ----------------------------------------------------------------------
# healthcare, continued. Appointment status is one of Completed,
# Scheduled, No-Show, Cancelled -- all four are present in the data.
# ----------------------------------------------------------------------

APPOINTMENT_STATUSES = ["Completed", "Scheduled", "No-Show", "Cancelled"]

HEALTHCARE_MORE: List[Dict[str, Any]] = [
    {
        "title": "Appointment status",
        "description": "Completed against missed, and how it moves month to month.",
        "parameters": [period(*HC_PERIOD)],
        "tiles": [
            metric("Appointments", """SELECT COUNT(*) FROM appointments
                   WHERE appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Completed", """SELECT COUNT(*) FROM appointments
                   WHERE status = 'Completed'
                     AND appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("No-shows", """SELECT COUNT(*) FROM appointments
                   WHERE status = 'No-Show'
                     AND appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Cancelled", """SELECT COUNT(*) FROM appointments
                   WHERE status = 'Cancelled'
                     AND appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Status by month", """SELECT TO_CHAR(DATE_TRUNC('month', appointmentdate), 'YYYY-MM') AS month,
                         status, COUNT(*) AS appointments
                  FROM appointments
                  WHERE appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY 1""",
                  {"type": "bar", "x": "month", "y": ["appointments"],
                   "color_by": "status", "stacked": True}, y=3),
            table("No-show rate by visit type", """SELECT visittype AS visit_type,
                         COUNT(*) AS appointments,
                         COUNT(*) FILTER (WHERE status = 'No-Show') AS no_shows,
                         ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'No-Show') / COUNT(*), 1) AS no_show_percent
                  FROM appointments
                  WHERE appointmentdate BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY no_show_percent DESC""", y=9),
        ],
    },
    {
        "title": "Patients and demographics",
        "description": "Who is registered, and when they registered.",
        "parameters": [],
        "tiles": [
            metric("Patients", "SELECT COUNT(*) FROM patients", 0),
            metric("Blood types recorded",
                   "SELECT COUNT(DISTINCT bloodtype) FROM patients", 3),
            metric("With an allergy on file",
                   "SELECT COUNT(DISTINCT patientid) FROM allergies", 6),
            metric("With a medical record",
                   "SELECT COUNT(DISTINCT patientid) FROM medicalrecords", 9),
            chart("Registrations by month", """SELECT TO_CHAR(DATE_TRUNC('month', registrationdate), 'YYYY-MM') AS month,
                         COUNT(*) AS patients
                  FROM patients GROUP BY 1 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["patients"]}, y=3),
            chart("Blood types", """SELECT bloodtype AS blood_type, COUNT(*) AS patients
                  FROM patients GROUP BY 1""",
                  {"type": "bar", "x": "blood_type", "y": ["patients"],
                   "sort_by": "patients", "descending": True}, y=9, width=6),
            chart("Gender", """SELECT gender, COUNT(*) AS patients FROM patients GROUP BY 1""",
                  {"type": "pie", "x": "gender", "y": ["patients"]}, y=9, x=6, width=6),
        ],
    },
    {
        "title": "Staff roster",
        "description": "Clinical staff by department and role, and who is not currently active.",
        "parameters": [],
        "tiles": [
            metric("Medical staff", "SELECT COUNT(*) FROM medicalstaff", 0),
            metric("Active", "SELECT COUNT(*) FROM medicalstaff WHERE status = 'Active'", 3),
            metric("On leave", "SELECT COUNT(*) FROM medicalstaff WHERE status = 'On Leave'", 6),
            metric("Departments", "SELECT COUNT(*) FROM departments", 9),
            chart("Staff per department", """SELECT d.departmentname AS department, COUNT(*) AS staff
                  FROM medicalstaff ms JOIN departments d ON d.departmentid = ms.departmentid
                  GROUP BY 1""",
                  {"type": "bar", "x": "department", "y": ["staff"],
                   "sort_by": "staff", "descending": True}, y=3, width=7),
            chart("Roles", """SELECT role, COUNT(*) AS staff FROM medicalstaff GROUP BY 1""",
                  {"type": "pie", "x": "role", "y": ["staff"],
                   "sort_by": "staff", "descending": True, "limit": 8}, y=3, x=7, width=5),
            table("Roster", """SELECT ms.firstname || ' ' || ms.lastname AS name, ms.role,
                         d.departmentname AS department, ms.status, ms.hiredate
                  FROM medicalstaff ms LEFT JOIN departments d ON d.departmentid = ms.departmentid
                  ORDER BY d.departmentname, ms.lastname""", y=9),
        ],
    },
    {
        "title": "Allergies and diagnoses",
        "description": "What the clinic is treating, and what patients react to.",
        "parameters": [top(12)],
        "tiles": [
            chart("Most common allergies", """SELECT allergy, COUNT(*) AS patients
                  FROM allergies GROUP BY 1""",
                  {"type": "bar", "x": "allergy", "y": ["patients"],
                   "sort_by": "patients", "descending": True, "limit": 12}, y=0, width=6),
            chart("Most common diagnoses", """SELECT diagnosis, COUNT(*) AS records
                  FROM medicalrecords GROUP BY 1""",
                  {"type": "bar", "x": "diagnosis", "y": ["records"],
                   "sort_by": "records", "descending": True, "limit": 12}, y=0, x=6, width=6),
            table("Diagnoses in detail", """SELECT diagnosis, COUNT(*) AS records,
                         COUNT(DISTINCT patientid) AS patients,
                         MAX(lastvisitdate) AS most_recent_visit
                  FROM medicalrecords GROUP BY 1
                  ORDER BY records DESC LIMIT {{ top }}""", y=6),
        ],
    },
]


# ----------------------------------------------------------------------
# ecommerce -- storefront. 830 orders between 2006-07-04 and 2008-05-06,
# every one priced in USD. Order status is delivered / processing /
# pending / cancelled; payment status is captured or pending. Verified
# against the live warehouse, and the lopsidedness is real: 809 of the
# 830 orders are delivered, so a status chart is mostly one bar.
# ----------------------------------------------------------------------

ECOM_PERIOD = ("2006-07-01", "2008-05-31")
ORDER_STATUSES = ["delivered", "processing", "pending", "cancelled"]
PAYMENT_PROVIDERS = ["card", "bank_transfer", "paypal"]

ECOMMERCE: List[Dict[str, Any]] = [
    {
        "title": "Sales overview",
        "description": "Headline trading figures and the monthly trend.",
        "parameters": [period(*ECOM_PERIOD)],
        "tiles": [
            metric("Revenue", """SELECT ROUND(SUM(grand_total)::numeric, 2) FROM orders
                   WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Orders", """SELECT COUNT(*) FROM orders
                   WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Average order value", """SELECT ROUND(AVG(grand_total)::numeric, 2) FROM orders
                   WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Customers who ordered", """SELECT COUNT(DISTINCT user_id) FROM orders
                   WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Revenue by month", """SELECT TO_CHAR(DATE_TRUNC('month', ordered_at), 'YYYY-MM') AS month,
                         ROUND(SUM(grand_total)::numeric, 2) AS revenue
                  FROM orders
                  WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["revenue"],
                   "y_label": "Revenue (USD)"}, y=3),
            table("Month by month", """SELECT TO_CHAR(DATE_TRUNC('month', ordered_at), 'YYYY-MM') AS month,
                         COUNT(*) AS orders,
                         ROUND(SUM(grand_total)::numeric, 2) AS revenue,
                         ROUND(AVG(grand_total)::numeric, 2) AS average_order,
                         ROUND(SUM(shipping_total)::numeric, 2) AS shipping
                  FROM orders
                  WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""", y=9),
        ],
    },
    {
        "title": "Revenue by category",
        "description": "Line revenue attributed through variant and product to category.",
        "parameters": [period(*ECOM_PERIOD)],
        "tiles": [
            chart("Revenue by category", """SELECT c.name AS category,
                         ROUND(SUM(oi.line_total)::numeric, 2) AS revenue
                  FROM order_items oi
                  JOIN orders o ON o.order_id = oi.order_id
                  JOIN product_variants v ON v.variant_id = oi.variant_id
                  JOIN products p ON p.product_id = v.product_id
                  JOIN categories c ON c.category_id = p.category_id
                  WHERE o.ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "category", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True}, y=0, width=7),
            chart("Share of revenue", """SELECT c.name AS category,
                         ROUND(SUM(oi.line_total)::numeric, 2) AS revenue
                  FROM order_items oi
                  JOIN orders o ON o.order_id = oi.order_id
                  JOIN product_variants v ON v.variant_id = oi.variant_id
                  JOIN products p ON p.product_id = v.product_id
                  JOIN categories c ON c.category_id = p.category_id
                  WHERE o.ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "category", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True}, y=0, x=7, width=5),
            table("Category detail", """SELECT c.name AS category,
                         COUNT(DISTINCT o.order_id) AS orders,
                         SUM(oi.qty) AS units,
                         ROUND(SUM(oi.line_total)::numeric, 2) AS revenue
                  FROM order_items oi
                  JOIN orders o ON o.order_id = oi.order_id
                  JOIN product_variants v ON v.variant_id = oi.variant_id
                  JOIN products p ON p.product_id = v.product_id
                  JOIN categories c ON c.category_id = p.category_id
                  WHERE o.ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY revenue DESC""", y=6),
        ],
    },
    {
        "title": "Order status",
        "description": "Where orders sit. Note the mix is heavily delivered.",
        "parameters": [period(*ECOM_PERIOD)],
        "tiles": [
            metric("Delivered", """SELECT COUNT(*) FROM orders WHERE status = 'delivered'
                   AND ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Processing", """SELECT COUNT(*) FROM orders WHERE status = 'processing'
                   AND ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Pending", """SELECT COUNT(*) FROM orders WHERE status = 'pending'
                   AND ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Cancelled", """SELECT COUNT(*) FROM orders WHERE status = 'cancelled'
                   AND ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Status by month", """SELECT TO_CHAR(DATE_TRUNC('month', ordered_at), 'YYYY-MM') AS month,
                         status, COUNT(*) AS orders
                  FROM orders
                  WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY 1""",
                  {"type": "bar", "x": "month", "y": ["orders"],
                   "color_by": "status", "stacked": True}, y=3),
            table("Not yet delivered", """SELECT o.order_number, o.status,
                         o.ordered_at::date AS ordered_on,
                         o.ship_city AS city, o.ship_country_code AS country,
                         o.grand_total
                  FROM orders o
                  WHERE o.status <> 'delivered'
                    AND o.ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  ORDER BY o.ordered_at LIMIT 40""", y=9),
        ],
    },
    {
        "title": "Payments and providers",
        "description": "How customers pay, and what has not been captured.",
        "parameters": [period(*ECOM_PERIOD)],
        "tiles": [
            metric("Captured", """SELECT ROUND(SUM(amount)::numeric, 2) FROM payments
                   WHERE status = 'captured'
                     AND created_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Payments", """SELECT COUNT(*) FROM payments
                   WHERE created_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Still pending", """SELECT COUNT(*) FROM payments WHERE status = 'pending'
                   AND created_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Average payment", """SELECT ROUND(AVG(amount)::numeric, 2) FROM payments
                   WHERE created_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Amount by provider", """SELECT provider,
                         ROUND(SUM(amount)::numeric, 2) AS amount
                  FROM payments
                  WHERE created_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "provider", "y": ["amount"],
                   "sort_by": "amount", "descending": True}, y=3, width=6),
            chart("Payments by provider", """SELECT provider, COUNT(*) AS payments
                  FROM payments
                  WHERE created_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "provider", "y": ["payments"]}, y=3, x=6, width=6),
            table("Provider detail", """SELECT provider, status, COUNT(*) AS payments,
                         ROUND(SUM(amount)::numeric, 2) AS amount,
                         ROUND(AVG(amount)::numeric, 2) AS average
                  FROM payments
                  WHERE created_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY amount DESC""", y=9),
        ],
    },
    {
        "title": "Shipping performance",
        "description": "How long parcels take, and which carrier takes it.",
        "parameters": [period(*ECOM_PERIOD)],
        "tiles": [
            metric("Shipments", """SELECT COUNT(*) FROM shipments
                   WHERE shipped_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Delivered", """SELECT COUNT(*) FROM shipments
                   WHERE delivered_at IS NOT NULL
                     AND shipped_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Average days in transit",
                   """SELECT ROUND(AVG(EXTRACT(EPOCH FROM (delivered_at - shipped_at)) / 86400)::numeric, 2)
                      FROM shipments
                      WHERE delivered_at IS NOT NULL
                        AND shipped_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Still in transit", """SELECT COUNT(*) FROM shipments
                   WHERE delivered_at IS NULL
                     AND shipped_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Shipments by month", """SELECT TO_CHAR(DATE_TRUNC('month', shipped_at), 'YYYY-MM') AS month,
                         COUNT(*) AS shipments
                  FROM shipments
                  WHERE shipped_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["shipments"]}, y=3),
            table("Carrier detail", """SELECT ca.name AS carrier, COUNT(*) AS shipments,
                         ROUND(AVG(EXTRACT(EPOCH FROM (s.delivered_at - s.shipped_at)) / 86400)::numeric, 2) AS average_days,
                         COUNT(*) FILTER (WHERE s.delivered_at IS NULL) AS still_open
                  FROM shipments s JOIN carriers ca ON ca.carrier_id = s.carrier_id
                  WHERE s.shipped_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY shipments DESC""", y=9),
        ],
    },
    {
        "title": "Top products",
        "description": "What actually sells, by revenue and by units.",
        "parameters": [period(*ECOM_PERIOD), top(15)],
        "tiles": [
            chart("Revenue by product", """SELECT oi.product_name AS product,
                         ROUND(SUM(oi.line_total)::numeric, 2) AS revenue
                  FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
                  WHERE o.ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "product", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 15}, y=0),
            table("Product detail", """SELECT oi.product_name AS product, oi.sku,
                         SUM(oi.qty) AS units,
                         ROUND(AVG(oi.unit_price)::numeric, 2) AS average_price,
                         ROUND(SUM(oi.line_total)::numeric, 2) AS revenue
                  FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
                  WHERE o.ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Customer concentration",
        "description": "Which accounts the revenue actually comes from.",
        "parameters": [period(*ECOM_PERIOD), top(15)],
        "tiles": [
            chart("Revenue by customer", """SELECT u.email::text AS customer,
                         ROUND(SUM(o.grand_total)::numeric, 2) AS revenue
                  FROM orders o JOIN users u ON u.user_id = o.user_id
                  WHERE o.ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "customer", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 15}, y=0),
            table("Customer detail", """SELECT u.email::text AS customer,
                         COALESCE(u.company_name, '') AS company,
                         COUNT(*) AS orders,
                         ROUND(SUM(o.grand_total)::numeric, 2) AS revenue,
                         ROUND(AVG(o.grand_total)::numeric, 2) AS average_order,
                         MAX(o.ordered_at)::date AS last_order
                  FROM orders o JOIN users u ON u.user_id = o.user_id
                  WHERE o.ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Where orders ship",
        "description": "Destination country and city for the period.",
        "parameters": [period(*ECOM_PERIOD), top(20)],
        "tiles": [
            chart("Revenue by destination country", """SELECT ship_country_code AS country,
                         ROUND(SUM(grand_total)::numeric, 2) AS revenue
                  FROM orders
                  WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "country", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 15}, y=0, width=7),
            chart("Orders by country", """SELECT ship_country_code AS country, COUNT(*) AS orders
                  FROM orders
                  WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "country", "y": ["orders"],
                   "sort_by": "orders", "descending": True, "limit": 8}, y=0, x=7, width=5),
            table("Cities", """SELECT ship_city AS city, ship_country_code AS country,
                         COUNT(*) AS orders,
                         ROUND(SUM(grand_total)::numeric, 2) AS revenue
                  FROM orders
                  WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Inventory health",
        "description": "What is on hand against what is already promised.",
        "parameters": [top(20, "Rows to list")],
        "tiles": [
            metric("Variants tracked", "SELECT COUNT(*) FROM inventory", 0),
            metric("Units on hand", "SELECT SUM(quantity_on_hand) FROM inventory", 3),
            metric("Units reserved", "SELECT SUM(quantity_reserved) FROM inventory", 6),
            metric("Under 20 on hand",
                   "SELECT COUNT(*) FROM inventory WHERE quantity_on_hand < 20", 9),
            chart("On hand by category", """SELECT c.name AS category,
                         SUM(i.quantity_on_hand) AS on_hand,
                         SUM(i.quantity_reserved) AS reserved
                  FROM inventory i
                  JOIN product_variants v ON v.variant_id = i.variant_id
                  JOIN products p ON p.product_id = v.product_id
                  JOIN categories c ON c.category_id = p.category_id
                  GROUP BY 1""",
                  {"type": "bar", "x": "category", "y": ["on_hand", "reserved"],
                   "stacked": True, "sort_by": "on_hand", "descending": True}, y=3),
            table("Lowest stock first", """SELECT v.name AS variant, v.sku, c.name AS category,
                         i.quantity_on_hand AS on_hand, i.quantity_reserved AS reserved,
                         (i.quantity_on_hand - i.quantity_reserved) AS available
                  FROM inventory i
                  JOIN product_variants v ON v.variant_id = i.variant_id
                  JOIN products p ON p.product_id = v.product_id
                  JOIN categories c ON c.category_id = p.category_id
                  ORDER BY available ASC LIMIT {{ top }}""", y=9),
        ],
    },
    {
        "title": "Discount exposure",
        "description": "What discounting costs, at order level and at line level.",
        "parameters": [period(*ECOM_PERIOD), top(20)],
        "tiles": [
            metric("Discount given", """SELECT ROUND(SUM(discount_total)::numeric, 2) FROM orders
                   WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Orders discounted", """SELECT COUNT(*) FROM orders
                   WHERE discount_total > 0
                     AND ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Subtotal", """SELECT ROUND(SUM(subtotal)::numeric, 2) FROM orders
                   WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Lines discounted", """SELECT COUNT(*) FROM order_items oi
                   JOIN orders o ON o.order_id = oi.order_id
                   WHERE oi.discount > 0
                     AND o.ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Discount by month", """SELECT TO_CHAR(DATE_TRUNC('month', ordered_at), 'YYYY-MM') AS month,
                         ROUND(SUM(discount_total)::numeric, 2) AS discount
                  FROM orders
                  WHERE ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "bar", "x": "month", "y": ["discount"]}, y=3),
            table("Deepest discounts", """SELECT o.order_number, o.ordered_at::date AS ordered_on,
                         o.subtotal, o.discount_total, o.grand_total,
                         CASE WHEN o.subtotal > 0
                              THEN ROUND(100.0 * o.discount_total / o.subtotal, 1)
                              ELSE NULL END AS discount_percent
                  FROM orders o
                  WHERE o.ordered_at::date BETWEEN {{ period_start }} AND {{ period_end }}
                  ORDER BY discount_percent DESC NULLS LAST LIMIT {{ top }}""", y=9),
        ],
    },
]


# ----------------------------------------------------------------------
# employees -- the classic HR dataset, and by far the largest here:
# 300,024 employees and 2.84M salary rows. Hires run 1985-01-01 to
# 2000-01-28. "Current" is the sentinel to_date = 9999-01-01, not
# CURRENT_DATE: the SQL policy denies that function outright, so the
# sentinel is the only way to say it.
# ----------------------------------------------------------------------

EMP_PERIOD = ("1985-01-01", "2000-01-31")
DEPARTMENT_NAMES = [
    "Customer Service", "Development", "Finance", "Human Resources", "Marketing",
    "Production", "Quality Management", "Research", "Sales",
]
EMP_TITLES = [
    "Assistant Engineer", "Engineer", "Manager", "Senior Engineer", "Senior Staff",
    "Staff", "Technique Leader",
]

EMPLOYEES: List[Dict[str, Any]] = [
    {
        "title": "Headcount overview",
        "description": "The size of the company and how it was hired.",
        "parameters": [period(*EMP_PERIOD)],
        "tiles": [
            metric("Employees", "SELECT COUNT(*) FROM employees", 0),
            metric("Hired in period", """SELECT COUNT(*) FROM employees
                   WHERE hire_date BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Departments", "SELECT COUNT(*) FROM departments", 6),
            metric("Still in a department", """SELECT COUNT(DISTINCT emp_no) FROM dept_emp
                   WHERE to_date = DATE '9999-01-01'""", 9),
            chart("Hires by year", """SELECT EXTRACT(YEAR FROM hire_date)::int AS year,
                         COUNT(*) AS hires
                  FROM employees
                  WHERE hire_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "bar", "x": "year", "y": ["hires"]}, y=3),
            table("Hires by year and gender", """SELECT EXTRACT(YEAR FROM hire_date)::int AS year,
                         COUNT(*) AS hires,
                         COUNT(*) FILTER (WHERE gender = 'F') AS female,
                         COUNT(*) FILTER (WHERE gender = 'M') AS male
                  FROM employees
                  WHERE hire_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""", y=9),
        ],
    },
    {
        "title": "Salary by department",
        "description": "Current salaries, averaged by department.",
        "parameters": [],
        "tiles": [
            chart("Average current salary", """SELECT d.dept_name AS department,
                         ROUND(AVG(s.salary)) AS average_salary
                  FROM salaries s
                  JOIN dept_emp de ON de.emp_no = s.emp_no
                  JOIN departments d ON d.dept_no = de.dept_no
                  WHERE s.to_date = DATE '9999-01-01' AND de.to_date = DATE '9999-01-01'
                  GROUP BY 1""",
                  {"type": "bar", "x": "department", "y": ["average_salary"],
                   "sort_by": "average_salary", "descending": True,
                   "y_label": "Average salary"}, y=0),
            table("Department detail", """SELECT d.dept_name AS department,
                         COUNT(*) AS employees,
                         ROUND(AVG(s.salary)) AS average_salary,
                         MIN(s.salary) AS lowest,
                         MAX(s.salary) AS highest
                  FROM salaries s
                  JOIN dept_emp de ON de.emp_no = s.emp_no
                  JOIN departments d ON d.dept_no = de.dept_no
                  WHERE s.to_date = DATE '9999-01-01' AND de.to_date = DATE '9999-01-01'
                  GROUP BY 1 ORDER BY average_salary DESC""", y=6),
        ],
    },
    {
        "title": "Titles and seniority",
        "description": "The title ladder, as currently held.",
        "parameters": [],
        "tiles": [
            chart("Current title counts", """SELECT title, COUNT(*) AS employees
                  FROM titles WHERE to_date = DATE '9999-01-01' GROUP BY 1""",
                  {"type": "bar", "x": "title", "y": ["employees"],
                   "sort_by": "employees", "descending": True}, y=0, width=7),
            chart("Share by title", """SELECT title, COUNT(*) AS employees
                  FROM titles WHERE to_date = DATE '9999-01-01' GROUP BY 1""",
                  {"type": "pie", "x": "title", "y": ["employees"],
                   "sort_by": "employees", "descending": True}, y=0, x=7, width=5),
            table("Title detail", """SELECT t.title, COUNT(*) AS employees,
                         ROUND(AVG(s.salary)) AS average_salary
                  FROM titles t
                  JOIN salaries s ON s.emp_no = t.emp_no
                  WHERE t.to_date = DATE '9999-01-01' AND s.to_date = DATE '9999-01-01'
                  GROUP BY 1 ORDER BY average_salary DESC""", y=6),
        ],
    },
    {
        "title": "Gender split by department",
        "description": "One bar per department, split by gender rather than summed.",
        "parameters": [],
        "tiles": [
            chart("Headcount by department and gender", """SELECT d.dept_name AS department,
                         e.gender, COUNT(*) AS employees
                  FROM dept_emp de
                  JOIN departments d ON d.dept_no = de.dept_no
                  JOIN employees e ON e.emp_no = de.emp_no
                  WHERE de.to_date = DATE '9999-01-01'
                  GROUP BY 1, 2""",
                  {"type": "bar", "x": "department", "y": ["employees"],
                   "color_by": "gender", "stacked": True}, y=0),
            table("Department detail", """SELECT d.dept_name AS department,
                         COUNT(*) AS employees,
                         COUNT(*) FILTER (WHERE e.gender = 'F') AS female,
                         COUNT(*) FILTER (WHERE e.gender = 'M') AS male,
                         ROUND(100.0 * COUNT(*) FILTER (WHERE e.gender = 'F') / COUNT(*), 1) AS percent_female
                  FROM dept_emp de
                  JOIN departments d ON d.dept_no = de.dept_no
                  JOIN employees e ON e.emp_no = de.emp_no
                  WHERE de.to_date = DATE '9999-01-01'
                  GROUP BY 1 ORDER BY employees DESC""", y=6),
        ],
    },
    {
        "title": "Hiring over time",
        "description": "Hiring by year, with one line per gender.",
        "parameters": [period(*EMP_PERIOD)],
        "tiles": [
            chart("Hires by year and gender", """SELECT EXTRACT(YEAR FROM hire_date)::int AS year,
                         gender, COUNT(*) AS hires
                  FROM employees
                  WHERE hire_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY 1""",
                  {"type": "line", "x": "year", "y": ["hires"], "color_by": "gender"}, y=0),
            table("Hiring by department", """SELECT d.dept_name AS department,
                         COUNT(*) AS joined_in_period
                  FROM dept_emp de
                  JOIN departments d ON d.dept_no = de.dept_no
                  WHERE de.from_date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY joined_in_period DESC""", y=6),
        ],
    },
    {
        "title": "Managers",
        "description": "Who has managed which department, and for how long.",
        "parameters": [],
        "tiles": [
            metric("Manager assignments", "SELECT COUNT(*) FROM dept_manager", 0),
            metric("Currently managing", """SELECT COUNT(*) FROM dept_manager
                   WHERE to_date = DATE '9999-01-01'""", 3),
            metric("Departments covered",
                   "SELECT COUNT(DISTINCT dept_no) FROM dept_manager", 6),
            metric("Average manager salary", """SELECT ROUND(AVG(s.salary)) FROM dept_manager dm
                   JOIN salaries s ON s.emp_no = dm.emp_no
                   WHERE dm.to_date = DATE '9999-01-01' AND s.to_date = DATE '9999-01-01'""", 9),
            chart("Managers per department", """SELECT d.dept_name AS department,
                         COUNT(*) AS managers
                  FROM dept_manager dm JOIN departments d ON d.dept_no = dm.dept_no
                  GROUP BY 1""",
                  {"type": "bar", "x": "department", "y": ["managers"],
                   "sort_by": "managers", "descending": True}, y=3),
            table("Every manager", """SELECT d.dept_name AS department,
                         e.first_name || ' ' || e.last_name AS manager,
                         dm.from_date, dm.to_date,
                         CASE WHEN dm.to_date = DATE '9999-01-01' THEN 'current' ELSE 'past' END AS standing
                  FROM dept_manager dm
                  JOIN departments d ON d.dept_no = dm.dept_no
                  JOIN employees e ON e.emp_no = dm.emp_no
                  ORDER BY d.dept_name, dm.from_date""", y=9),
        ],
    },
    {
        "title": "Salary distribution",
        "description": "How current salaries are spread, in bands.",
        "parameters": [],
        "tiles": [
            metric("Lowest current",
                   "SELECT MIN(salary) FROM salaries WHERE to_date = DATE '9999-01-01'", 0),
            metric("Average current",
                   "SELECT ROUND(AVG(salary)) FROM salaries WHERE to_date = DATE '9999-01-01'", 3),
            metric("Highest current",
                   "SELECT MAX(salary) FROM salaries WHERE to_date = DATE '9999-01-01'", 6),
            metric("Salary rows",
                   "SELECT COUNT(*) FROM salaries WHERE to_date = DATE '9999-01-01'", 9),
            chart("Employees per salary band", """SELECT CASE
                           WHEN salary < 50000 THEN 'Under 50k'
                           WHEN salary < 70000 THEN '50k to 70k'
                           WHEN salary < 90000 THEN '70k to 90k'
                           WHEN salary < 110000 THEN '90k to 110k'
                           ELSE '110k and over' END AS band,
                         COUNT(*) AS employees
                  FROM salaries WHERE to_date = DATE '9999-01-01'
                  GROUP BY 1 ORDER BY MIN(salary)""",
                  {"type": "bar", "x": "band", "y": ["employees"]}, y=3),
        ],
    },
    {
        "title": "Tenure",
        "description": "How long people have been here, by hire cohort.",
        "parameters": [],
        "tiles": [
            chart("Employees by hire decade", """SELECT CASE
                           WHEN hire_date < DATE '1990-01-01' THEN '1985 to 1989'
                           WHEN hire_date < DATE '1995-01-01' THEN '1990 to 1994'
                           ELSE '1995 onwards' END AS cohort,
                         COUNT(*) AS employees
                  FROM employees GROUP BY 1 ORDER BY MIN(hire_date)""",
                  {"type": "bar", "x": "cohort", "y": ["employees"]}, y=0, width=6),
            chart("Average salary by hire cohort", """SELECT CASE
                           WHEN e.hire_date < DATE '1990-01-01' THEN '1985 to 1989'
                           WHEN e.hire_date < DATE '1995-01-01' THEN '1990 to 1994'
                           ELSE '1995 onwards' END AS cohort,
                         ROUND(AVG(s.salary)) AS average_salary
                  FROM employees e JOIN salaries s ON s.emp_no = e.emp_no
                  WHERE s.to_date = DATE '9999-01-01'
                  GROUP BY 1 ORDER BY MIN(e.hire_date)""",
                  {"type": "bar", "x": "cohort", "y": ["average_salary"]}, y=0, x=6, width=6),
            table("Longest serving", """SELECT e.first_name || ' ' || e.last_name AS employee,
                         e.hire_date, t.title, s.salary AS current_salary
                  FROM employees e
                  JOIN titles t ON t.emp_no = e.emp_no AND t.to_date = DATE '9999-01-01'
                  JOIN salaries s ON s.emp_no = e.emp_no AND s.to_date = DATE '9999-01-01'
                  ORDER BY e.hire_date ASC LIMIT 25""", y=6),
        ],
    },
    {
        "title": "Department moves",
        "description": "People who have belonged to more than one department.",
        "parameters": [top(25, "Rows to list")],
        "tiles": [
            metric("Assignment rows", "SELECT COUNT(*) FROM dept_emp", 0),
            metric("Employees", "SELECT COUNT(DISTINCT emp_no) FROM dept_emp", 3),
            metric("Moved at least once", """SELECT COUNT(*) FROM (
                     SELECT emp_no FROM dept_emp GROUP BY emp_no HAVING COUNT(*) > 1
                   ) AS movers""", 6),
            metric("Closed assignments", """SELECT COUNT(*) FROM dept_emp
                   WHERE to_date <> DATE '9999-01-01'""", 9),
            chart("Departures by department", """SELECT d.dept_name AS department,
                         COUNT(*) AS closed_assignments
                  FROM dept_emp de JOIN departments d ON d.dept_no = de.dept_no
                  WHERE de.to_date <> DATE '9999-01-01'
                  GROUP BY 1""",
                  {"type": "bar", "x": "department", "y": ["closed_assignments"],
                   "sort_by": "closed_assignments", "descending": True}, y=3),
            table("Who moved", """SELECT e.first_name || ' ' || e.last_name AS employee,
                         COUNT(*) AS departments,
                         MIN(de.from_date) AS first_assignment,
                         MAX(de.to_date) AS last_assignment
                  FROM dept_emp de JOIN employees e ON e.emp_no = de.emp_no
                  GROUP BY 1 HAVING COUNT(*) > 1
                  ORDER BY departments DESC, employee LIMIT {{ top }}""", y=9),
        ],
    },
    {
        "title": "Top earners",
        "description": "The highest current salaries, and where they sit.",
        "parameters": [top(25)],
        "tiles": [
            chart("Highest current salaries", """SELECT e.first_name || ' ' || e.last_name AS employee,
                         s.salary
                  FROM salaries s JOIN employees e ON e.emp_no = s.emp_no
                  WHERE s.to_date = DATE '9999-01-01'
                  ORDER BY s.salary DESC LIMIT 20""",
                  {"type": "bar", "x": "employee", "y": ["salary"],
                   "sort_by": "salary", "descending": True}, y=0),
            table("Top earners in detail", """SELECT e.first_name || ' ' || e.last_name AS employee,
                         t.title, d.dept_name AS department, s.salary, e.hire_date
                  FROM salaries s
                  JOIN employees e ON e.emp_no = s.emp_no
                  LEFT JOIN titles t ON t.emp_no = s.emp_no AND t.to_date = DATE '9999-01-01'
                  LEFT JOIN dept_emp de ON de.emp_no = s.emp_no AND de.to_date = DATE '9999-01-01'
                  LEFT JOIN departments d ON d.dept_no = de.dept_no
                  WHERE s.to_date = DATE '9999-01-01'
                  ORDER BY s.salary DESC LIMIT {{ top }}""", y=6),
        ],
    },
]


# ----------------------------------------------------------------------
# booking -- two datasets in one workspace, and they are not the same size.
# `hotel_bookings` is the analytical one: 119,390 rows, arrivals across
# 2015-2017, reservation_status_date from 2014-10-17 to 2017-09-14.
# `reservation`/`room`/`guest` are the operational side and are tiny -- 24
# reservations, 18 rooms, 11 guests -- so they get one report, not five.
#
# arrival_date_month holds month *names*, so ordering by it alphabetically
# puts April first. The CASE below is the price of a readable axis.
# ----------------------------------------------------------------------

BOOK_PERIOD = ("2014-10-17", "2017-09-14")
HOTELS = ["City Hotel", "Resort Hotel"]
MARKET_SEGMENTS = [
    "Online TA", "Offline TA/TO", "Groups", "Direct", "Corporate",
    "Complementary", "Aviation",
]
CUSTOMER_TYPES = ["Transient", "Transient-Party", "Contract", "Group"]

_MONTH_ORDER = """CASE arrival_date_month
        WHEN 'January' THEN 1 WHEN 'February' THEN 2 WHEN 'March' THEN 3
        WHEN 'April' THEN 4 WHEN 'May' THEN 5 WHEN 'June' THEN 6
        WHEN 'July' THEN 7 WHEN 'August' THEN 8 WHEN 'September' THEN 9
        WHEN 'October' THEN 10 WHEN 'November' THEN 11 ELSE 12 END"""

BOOKING: List[Dict[str, Any]] = [
    {
        "title": "Bookings overview",
        "description": "Volume, cancellations and rate across the whole book.",
        "parameters": [choice("hotel", HOTELS, "City Hotel")],
        "tiles": [
            metric("Bookings", "SELECT COUNT(*) FROM hotel_bookings WHERE hotel = {{ hotel }}", 0),
            metric("Cancelled", """SELECT COUNT(*) FROM hotel_bookings
                   WHERE hotel = {{ hotel }} AND is_canceled = 1""", 3),
            metric("Average daily rate", """SELECT ROUND(AVG(adr)::numeric, 2) FROM hotel_bookings
                   WHERE hotel = {{ hotel }}""", 6),
            metric("Average lead time", """SELECT ROUND(AVG(lead_time)) FROM hotel_bookings
                   WHERE hotel = {{ hotel }}""", 9),
            chart("Bookings by arrival month", f"""SELECT arrival_date_month AS month,
                         COUNT(*) AS bookings
                  FROM hotel_bookings WHERE hotel = {{{{ hotel }}}}
                  GROUP BY 1 ORDER BY MIN({_MONTH_ORDER})""",
                  {"type": "bar", "x": "month", "y": ["bookings"]}, y=3),
            table("By year", """SELECT arrival_date_year AS year, COUNT(*) AS bookings,
                         COUNT(*) FILTER (WHERE is_canceled = 1) AS cancelled,
                         ROUND(AVG(adr)::numeric, 2) AS average_daily_rate,
                         ROUND(AVG(lead_time)) AS average_lead_time
                  FROM hotel_bookings WHERE hotel = {{ hotel }}
                  GROUP BY 1 ORDER BY 1""", y=9),
        ],
    },
    {
        "title": "Cancellations",
        "description": "Who cancels, and how the rate differs by segment.",
        "parameters": [],
        "tiles": [
            metric("Bookings", "SELECT COUNT(*) FROM hotel_bookings", 0),
            metric("Cancelled", "SELECT COUNT(*) FROM hotel_bookings WHERE is_canceled = 1", 3),
            metric("No-shows", """SELECT COUNT(*) FROM hotel_bookings
                   WHERE reservation_status = 'No-Show'""", 6),
            metric("Cancellation rate %", """SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE is_canceled = 1)
                          / COUNT(*), 1) FROM hotel_bookings""", 9),
            chart("Cancellation rate by segment", """SELECT market_segment AS segment,
                         ROUND(100.0 * COUNT(*) FILTER (WHERE is_canceled = 1) / COUNT(*), 1) AS cancel_percent
                  FROM hotel_bookings GROUP BY 1""",
                  {"type": "bar", "x": "segment", "y": ["cancel_percent"],
                   "sort_by": "cancel_percent", "descending": True,
                   "y_label": "Cancelled (%)"}, y=3),
            table("Segment detail", """SELECT market_segment AS segment, hotel,
                         COUNT(*) AS bookings,
                         COUNT(*) FILTER (WHERE is_canceled = 1) AS cancelled,
                         ROUND(100.0 * COUNT(*) FILTER (WHERE is_canceled = 1) / COUNT(*), 1) AS cancel_percent
                  FROM hotel_bookings GROUP BY 1, 2 ORDER BY bookings DESC""", y=9),
        ],
    },
    {
        "title": "Market segments",
        "description": "Where the business comes from, and what it pays.",
        "parameters": [],
        "tiles": [
            chart("Bookings by segment", """SELECT market_segment AS segment, COUNT(*) AS bookings
                  FROM hotel_bookings GROUP BY 1""",
                  {"type": "bar", "x": "segment", "y": ["bookings"],
                   "sort_by": "bookings", "descending": True}, y=0, width=7),
            chart("Share of bookings", """SELECT market_segment AS segment, COUNT(*) AS bookings
                  FROM hotel_bookings GROUP BY 1""",
                  {"type": "pie", "x": "segment", "y": ["bookings"],
                   "sort_by": "bookings", "descending": True, "limit": 6}, y=0, x=7, width=5),
            chart("Segment by distribution channel", """SELECT distribution_channel AS channel,
                         market_segment AS segment, COUNT(*) AS bookings
                  FROM hotel_bookings GROUP BY 1, 2""",
                  {"type": "bar", "x": "channel", "y": ["bookings"],
                   "color_by": "segment", "stacked": True}, y=6),
            table("Segment detail", """SELECT market_segment AS segment, COUNT(*) AS bookings,
                         ROUND(AVG(adr)::numeric, 2) AS average_daily_rate,
                         ROUND(AVG(lead_time)) AS average_lead_time,
                         ROUND(AVG(total_of_special_requests)::numeric, 2) AS average_requests
                  FROM hotel_bookings GROUP BY 1 ORDER BY bookings DESC""", y=12),
        ],
    },
    {
        "title": "Average daily rate",
        "description": "What a room actually earns per night, over the season.",
        "parameters": [choice("customer_type", CUSTOMER_TYPES, "Transient")],
        "tiles": [
            chart("Rate by month and hotel", f"""SELECT arrival_date_month AS month, hotel,
                         ROUND(AVG(adr)::numeric, 2) AS average_daily_rate
                  FROM hotel_bookings
                  WHERE customer_type = {{{{ customer_type }}}}
                  GROUP BY 1, 2 ORDER BY MIN({_MONTH_ORDER})""",
                  {"type": "line", "x": "month", "y": ["average_daily_rate"],
                   "color_by": "hotel", "y_label": "ADR"}, y=0),
            chart("Rate by room type assigned", """SELECT assigned_room_type AS room_type,
                         ROUND(AVG(adr)::numeric, 2) AS average_daily_rate
                  FROM hotel_bookings
                  WHERE customer_type = {{ customer_type }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "room_type", "y": ["average_daily_rate"],
                   "sort_by": "average_daily_rate", "descending": True}, y=6),
            table("Rate by deposit type", """SELECT deposit_type, COUNT(*) AS bookings,
                         ROUND(AVG(adr)::numeric, 2) AS average_daily_rate,
                         ROUND(MAX(adr)::numeric, 2) AS highest_rate
                  FROM hotel_bookings
                  WHERE customer_type = {{ customer_type }}
                  GROUP BY 1 ORDER BY bookings DESC""", y=12),
        ],
    },
    {
        "title": "Where guests come from",
        "description": "Country of origin. Portugal dominates; that is the dataset, not a bug.",
        "parameters": [top(15)],
        "tiles": [
            chart("Bookings by country", """SELECT country, COUNT(*) AS bookings
                  FROM hotel_bookings WHERE country IS NOT NULL GROUP BY 1""",
                  {"type": "bar", "x": "country", "y": ["bookings"],
                   "sort_by": "bookings", "descending": True, "limit": 15}, y=0, width=7),
            chart("Share by country", """SELECT country, COUNT(*) AS bookings
                  FROM hotel_bookings WHERE country IS NOT NULL GROUP BY 1""",
                  {"type": "pie", "x": "country", "y": ["bookings"],
                   "sort_by": "bookings", "descending": True, "limit": 8}, y=0, x=7, width=5),
            table("Country detail", """SELECT country, COUNT(*) AS bookings,
                         COUNT(*) FILTER (WHERE is_canceled = 1) AS cancelled,
                         ROUND(AVG(adr)::numeric, 2) AS average_daily_rate,
                         ROUND(AVG(lead_time)) AS average_lead_time
                  FROM hotel_bookings WHERE country IS NOT NULL
                  GROUP BY 1 ORDER BY bookings DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Lead time",
        "description": "How far ahead people book, and whether that predicts cancelling.",
        "parameters": [],
        "tiles": [
            metric("Average lead time", "SELECT ROUND(AVG(lead_time)) FROM hotel_bookings", 0),
            metric("Booked same day",
                   "SELECT COUNT(*) FROM hotel_bookings WHERE lead_time = 0", 3),
            metric("Booked a year ahead",
                   "SELECT COUNT(*) FROM hotel_bookings WHERE lead_time >= 365", 6),
            metric("Longest lead time", "SELECT MAX(lead_time) FROM hotel_bookings", 9),
            chart("Bookings by lead-time band", """SELECT CASE
                           WHEN lead_time = 0 THEN 'Same day'
                           WHEN lead_time < 8 THEN 'Within a week'
                           WHEN lead_time < 31 THEN 'Within a month'
                           WHEN lead_time < 91 THEN '1 to 3 months'
                           WHEN lead_time < 366 THEN '3 to 12 months'
                           ELSE 'Over a year' END AS band,
                         COUNT(*) AS bookings
                  FROM hotel_bookings GROUP BY 1 ORDER BY MIN(lead_time)""",
                  {"type": "bar", "x": "band", "y": ["bookings"]}, y=3),
            table("Cancellation by lead-time band", """SELECT CASE
                           WHEN lead_time = 0 THEN 'Same day'
                           WHEN lead_time < 8 THEN 'Within a week'
                           WHEN lead_time < 31 THEN 'Within a month'
                           WHEN lead_time < 91 THEN '1 to 3 months'
                           WHEN lead_time < 366 THEN '3 to 12 months'
                           ELSE 'Over a year' END AS band,
                         COUNT(*) AS bookings,
                         COUNT(*) FILTER (WHERE is_canceled = 1) AS cancelled,
                         ROUND(100.0 * COUNT(*) FILTER (WHERE is_canceled = 1) / COUNT(*), 1) AS cancel_percent
                  FROM hotel_bookings GROUP BY 1 ORDER BY MIN(lead_time)""", y=9),
        ],
    },
    {
        "title": "Party size and stay length",
        "description": "Who travels together, and for how many nights.",
        "parameters": [choice("hotel", HOTELS, "Resort Hotel")],
        "tiles": [
            metric("Average adults", """SELECT ROUND(AVG(adults)::numeric, 2) FROM hotel_bookings
                   WHERE hotel = {{ hotel }}""", 0),
            metric("Bookings with children", """SELECT COUNT(*) FROM hotel_bookings
                   WHERE hotel = {{ hotel }} AND children > 0""", 3),
            metric("Average week nights", """SELECT ROUND(AVG(stays_in_week_nights)::numeric, 2)
                   FROM hotel_bookings WHERE hotel = {{ hotel }}""", 6),
            metric("Average weekend nights", """SELECT ROUND(AVG(stays_in_weekend_nights)::numeric, 2)
                   FROM hotel_bookings WHERE hotel = {{ hotel }}""", 9),
            chart("Bookings by total nights", """SELECT (stays_in_week_nights + stays_in_weekend_nights) AS nights,
                         COUNT(*) AS bookings
                  FROM hotel_bookings
                  WHERE hotel = {{ hotel }}
                    AND (stays_in_week_nights + stays_in_weekend_nights) BETWEEN 1 AND 14
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "bar", "x": "nights", "y": ["bookings"],
                   "x_label": "Nights"}, y=3),
            table("Party composition", """SELECT adults, COUNT(*) AS bookings,
                         COUNT(*) FILTER (WHERE children > 0) AS with_children,
                         COUNT(*) FILTER (WHERE babies > 0) AS with_babies,
                         ROUND(AVG(adr)::numeric, 2) AS average_daily_rate
                  FROM hotel_bookings
                  WHERE hotel = {{ hotel }} AND adults BETWEEN 1 AND 6
                  GROUP BY 1 ORDER BY 1""", y=9),
        ],
    },
    {
        "title": "Special requests and parking",
        "description": "What guests ask for on top of the room.",
        "parameters": [],
        "tiles": [
            metric("Asked for parking",
                   "SELECT COUNT(*) FROM hotel_bookings WHERE required_car_parking_spaces > 0", 0),
            metric("Made a special request",
                   "SELECT COUNT(*) FROM hotel_bookings WHERE total_of_special_requests > 0", 3),
            metric("Repeat guests",
                   "SELECT COUNT(*) FROM hotel_bookings WHERE is_repeated_guest = 1", 6),
            metric("Changed their booking",
                   "SELECT COUNT(*) FROM hotel_bookings WHERE booking_changes > 0", 9),
            chart("Requests per booking", """SELECT total_of_special_requests AS requests,
                         COUNT(*) AS bookings
                  FROM hotel_bookings GROUP BY 1 ORDER BY 1""",
                  {"type": "bar", "x": "requests", "y": ["bookings"]}, y=3, width=6),
            chart("Meal plans", """SELECT meal, COUNT(*) AS bookings
                  FROM hotel_bookings GROUP BY 1""",
                  {"type": "pie", "x": "meal", "y": ["bookings"],
                   "sort_by": "bookings", "descending": True}, y=3, x=6, width=6),
            table("Requests against cancelling", """SELECT total_of_special_requests AS requests,
                         COUNT(*) AS bookings,
                         COUNT(*) FILTER (WHERE is_canceled = 1) AS cancelled,
                         ROUND(100.0 * COUNT(*) FILTER (WHERE is_canceled = 1) / COUNT(*), 1) AS cancel_percent
                  FROM hotel_bookings GROUP BY 1 ORDER BY 1""", y=9),
        ],
    },
    {
        "title": "Rooms and amenities",
        "description": "The property itself: 18 rooms, and what is in them.",
        "parameters": [],
        "tiles": [
            metric("Rooms", "SELECT COUNT(*) FROM room", 0),
            metric("Accessible rooms", "SELECT COUNT(*) FROM room WHERE isada", 3),
            metric("With a jacuzzi", "SELECT COUNT(*) FROM room WHERE hasjacuzzi", 6),
            metric("Average base price",
                   "SELECT ROUND(AVG(baseprice)::numeric, 2) FROM room", 9),
            chart("Rooms by type", """SELECT roomtype AS room_type, COUNT(*) AS rooms
                  FROM room GROUP BY 1""",
                  {"type": "bar", "x": "room_type", "y": ["rooms"],
                   "sort_by": "rooms", "descending": True}, y=3, width=6),
            chart("Amenities fitted", """SELECT a.amenitytype AS amenity, COUNT(*) AS rooms
                  FROM roomamenity ra JOIN amenity a ON a.amenityid = ra.amenityid
                  GROUP BY 1""",
                  {"type": "bar", "x": "amenity", "y": ["rooms"],
                   "sort_by": "rooms", "descending": True}, y=3, x=6, width=6),
            table("Every room", """SELECT roomnumber AS room, roomtype AS room_type,
                         baseprice AS base_price, extraperson AS extra_person,
                         standardoccupancy AS standard_occupancy,
                         maximumoccupancy AS maximum_occupancy,
                         isada AS accessible, hasjacuzzi AS jacuzzi
                  FROM room ORDER BY roomnumber""", y=9),
        ],
    },
    {
        "title": "Reservations ledger",
        "description": "The operational side: 24 reservations across 2023, and who made them.",
        "parameters": [],
        "tiles": [
            metric("Reservations", "SELECT COUNT(*) FROM reservation", 0),
            metric("Guests on file", "SELECT COUNT(*) FROM guest", 3),
            metric("Total booked",
                   "SELECT ROUND(SUM(total)::numeric, 2) FROM reservation", 6),
            metric("Average reservation",
                   "SELECT ROUND(AVG(total)::numeric, 2) FROM reservation", 9),
            chart("Reservation value by room type", """SELECT ro.roomtype AS room_type,
                         ROUND(SUM(r.total)::numeric, 2) AS booked
                  FROM roomreservation rr
                  JOIN room ro ON ro.roomnumber = rr.roomnumber
                  JOIN reservation r ON r.reservationid = rr.reservationid
                  GROUP BY 1""",
                  {"type": "bar", "x": "room_type", "y": ["booked"],
                   "sort_by": "booked", "descending": True}, y=3),
            table("Every reservation", """SELECT r.reservationid AS reservation,
                         r.checkindate AS check_in, r.checkoutdate AS check_out,
                         r.adults, r.children, r.total,
                         g.firstname || ' ' || g.lastname AS guest, g.city, g.state
                  FROM reservation r
                  LEFT JOIN guestreservation gr ON gr.reservationid = r.reservationid
                  LEFT JOIN guest g ON g.guestid = gr.guestid
                  ORDER BY r.checkindate""", y=9),
        ],
    },
]


# ----------------------------------------------------------------------
# pagila -- DVD rental. Payments run 2022-01-23 to 2026-07-28 and total
# 170,962.39 across 51,061 rows; rentals 51,805 with 241 still out.
#
# One trap, found by asking the database rather than reading the schema:
# `staff` holds 1,500 rows across 475 distinct store_id values, which is
# synthetic bulk and does not describe two shops. Anything per-store here
# comes from customer.store_id, which really is just 1 and 2.
# ----------------------------------------------------------------------

PAGILA_PERIOD = ("2022-01-01", "2026-07-31")
FILM_RATINGS = ["G", "PG", "PG-13", "R", "NC-17"]

PAGILA: List[Dict[str, Any]] = [
    {
        "title": "Rental revenue overview",
        "description": "Takings and volume, over a period you choose.",
        "parameters": [period(*PAGILA_PERIOD)],
        "tiles": [
            metric("Revenue", """SELECT ROUND(SUM(amount)::numeric, 2) FROM payment
                   WHERE payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}""", 0),
            metric("Payments", """SELECT COUNT(*) FROM payment
                   WHERE payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}""", 3),
            metric("Average payment", """SELECT ROUND(AVG(amount)::numeric, 2) FROM payment
                   WHERE payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}""", 6),
            metric("Paying customers", """SELECT COUNT(DISTINCT customer_id) FROM payment
                   WHERE payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}""", 9),
            chart("Revenue by month", """SELECT TO_CHAR(DATE_TRUNC('month', payment_date), 'YYYY-MM') AS month,
                         ROUND(SUM(amount)::numeric, 2) AS revenue
                  FROM payment
                  WHERE payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["revenue"]}, y=3),
            table("Month by month", """SELECT TO_CHAR(DATE_TRUNC('month', payment_date), 'YYYY-MM') AS month,
                         COUNT(*) AS payments,
                         ROUND(SUM(amount)::numeric, 2) AS revenue,
                         ROUND(AVG(amount)::numeric, 2) AS average_payment
                  FROM payment
                  WHERE payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY 1""", y=9),
        ],
    },
    {
        "title": "Revenue by category",
        "description": "Takings traced from payment through rental and inventory to film category.",
        "parameters": [period(*PAGILA_PERIOD)],
        "tiles": [
            chart("Revenue by category", """SELECT c.name AS category,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue
                  FROM payment p
                  JOIN rental r ON r.rental_id = p.rental_id
                  JOIN inventory i ON i.inventory_id = r.inventory_id
                  JOIN film_category fc ON fc.film_id = i.film_id
                  JOIN category c ON c.category_id = fc.category_id
                  WHERE p.payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "category", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True}, y=0, width=7),
            chart("Share by category", """SELECT c.name AS category,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue
                  FROM payment p
                  JOIN rental r ON r.rental_id = p.rental_id
                  JOIN inventory i ON i.inventory_id = r.inventory_id
                  JOIN film_category fc ON fc.film_id = i.film_id
                  JOIN category c ON c.category_id = fc.category_id
                  WHERE p.payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "pie", "x": "category", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 8}, y=0, x=7, width=5),
            table("Category detail", """SELECT c.name AS category,
                         COUNT(*) AS payments,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue,
                         ROUND(AVG(p.amount)::numeric, 2) AS average_payment
                  FROM payment p
                  JOIN rental r ON r.rental_id = p.rental_id
                  JOIN inventory i ON i.inventory_id = r.inventory_id
                  JOIN film_category fc ON fc.film_id = i.film_id
                  JOIN category c ON c.category_id = fc.category_id
                  WHERE p.payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY revenue DESC""", y=6),
        ],
    },
    {
        "title": "Film ratings",
        "description": "What the catalogue is rated, and what earns.",
        "parameters": [],
        "tiles": [
            chart("Films by rating", """SELECT rating::text AS rating, COUNT(*) AS films
                  FROM film GROUP BY 1""",
                  {"type": "bar", "x": "rating", "y": ["films"],
                   "sort_by": "films", "descending": True}, y=0, width=6),
            chart("Revenue by rating", """SELECT f.rating::text AS rating,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue
                  FROM payment p
                  JOIN rental r ON r.rental_id = p.rental_id
                  JOIN inventory i ON i.inventory_id = r.inventory_id
                  JOIN film f ON f.film_id = i.film_id
                  GROUP BY 1""",
                  {"type": "pie", "x": "rating", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True}, y=0, x=6, width=6),
            table("Rating detail", """SELECT rating::text AS rating, COUNT(*) AS films,
                         ROUND(AVG(rental_rate)::numeric, 2) AS average_rental_rate,
                         ROUND(AVG(length)) AS average_minutes,
                         ROUND(AVG(replacement_cost)::numeric, 2) AS average_replacement_cost
                  FROM film GROUP BY 1 ORDER BY films DESC""", y=6),
        ],
    },
    {
        "title": "Top films",
        "description": "The titles that actually earn, over a period you choose.",
        "parameters": [period(*PAGILA_PERIOD), top(15)],
        "tiles": [
            chart("Revenue by film", """SELECT f.title AS film,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue
                  FROM payment p
                  JOIN rental r ON r.rental_id = p.rental_id
                  JOIN inventory i ON i.inventory_id = r.inventory_id
                  JOIN film f ON f.film_id = i.film_id
                  WHERE p.payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1""",
                  {"type": "bar", "x": "film", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 15}, y=0),
            table("Film detail", """SELECT f.title AS film, f.rating::text AS rating,
                         COUNT(*) AS rentals_paid,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue
                  FROM payment p
                  JOIN rental r ON r.rental_id = p.rental_id
                  JOIN inventory i ON i.inventory_id = r.inventory_id
                  JOIN film f ON f.film_id = i.film_id
                  WHERE p.payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Top actors",
        "description": "Who appears most, and what their films take.",
        "parameters": [top(15)],
        "tiles": [
            chart("Films per actor", """SELECT a.first_name || ' ' || a.last_name AS actor,
                         COUNT(*) AS films
                  FROM film_actor fa JOIN actor a ON a.actor_id = fa.actor_id
                  GROUP BY 1""",
                  {"type": "bar", "x": "actor", "y": ["films"],
                   "sort_by": "films", "descending": True, "limit": 15}, y=0),
            table("Actor detail", """SELECT a.first_name || ' ' || a.last_name AS actor,
                         COUNT(DISTINCT fa.film_id) AS films,
                         ROUND(AVG(f.rental_rate)::numeric, 2) AS average_rental_rate,
                         ROUND(AVG(f.length)) AS average_minutes
                  FROM film_actor fa
                  JOIN actor a ON a.actor_id = fa.actor_id
                  JOIN film f ON f.film_id = fa.film_id
                  GROUP BY 1 ORDER BY films DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Customers by country",
        "description": "Where the customer base is, through address and city.",
        "parameters": [top(15)],
        "tiles": [
            chart("Revenue by country", """SELECT co.country,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue
                  FROM payment p
                  JOIN customer cu ON cu.customer_id = p.customer_id
                  JOIN address a ON a.address_id = cu.address_id
                  JOIN city ci ON ci.city_id = a.city_id
                  JOIN country co ON co.country_id = ci.country_id
                  GROUP BY 1""",
                  {"type": "bar", "x": "country", "y": ["revenue"],
                   "sort_by": "revenue", "descending": True, "limit": 15}, y=0),
            table("Country detail", """SELECT co.country,
                         COUNT(DISTINCT cu.customer_id) AS customers,
                         COUNT(*) AS payments,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue
                  FROM payment p
                  JOIN customer cu ON cu.customer_id = p.customer_id
                  JOIN address a ON a.address_id = cu.address_id
                  JOIN city ci ON ci.city_id = a.city_id
                  JOIN country co ON co.country_id = ci.country_id
                  GROUP BY 1 ORDER BY revenue DESC LIMIT {{ top }}""", y=6),
        ],
    },
    {
        "title": "Store comparison",
        "description": "The two shops side by side, taken from customer.store_id.",
        "parameters": [period(*PAGILA_PERIOD)],
        "tiles": [
            chart("Revenue by store and month", """SELECT TO_CHAR(DATE_TRUNC('month', p.payment_date), 'YYYY-MM') AS month,
                         'Store ' || cu.store_id::text AS store,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue
                  FROM payment p JOIN customer cu ON cu.customer_id = p.customer_id
                  WHERE p.payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1, 2 ORDER BY 1""",
                  {"type": "line", "x": "month", "y": ["revenue"], "color_by": "store"}, y=0),
            table("Store detail", """SELECT 'Store ' || cu.store_id::text AS store,
                         COUNT(DISTINCT cu.customer_id) AS customers,
                         COUNT(*) AS payments,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue,
                         ROUND(AVG(p.amount)::numeric, 2) AS average_payment
                  FROM payment p JOIN customer cu ON cu.customer_id = p.customer_id
                  WHERE p.payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY revenue DESC""", y=6),
        ],
    },
    {
        "title": "Outstanding rentals",
        "description": "Discs that have gone out and not come back.",
        "parameters": [top(25, "Rows to list")],
        "tiles": [
            metric("Rentals", "SELECT COUNT(*) FROM rental", 0),
            metric("Still out", "SELECT COUNT(*) FROM rental WHERE return_date IS NULL", 3),
            metric("Returned", "SELECT COUNT(*) FROM rental WHERE return_date IS NOT NULL", 6),
            metric("Average days out",
                   """SELECT ROUND(AVG(EXTRACT(EPOCH FROM (return_date - rental_date)) / 86400)::numeric, 2)
                      FROM rental WHERE return_date IS NOT NULL""", 9),
            chart("Open rentals by month taken out", """SELECT TO_CHAR(DATE_TRUNC('month', rental_date), 'YYYY-MM') AS month,
                         COUNT(*) AS still_out
                  FROM rental WHERE return_date IS NULL
                  GROUP BY 1 ORDER BY 1""",
                  {"type": "bar", "x": "month", "y": ["still_out"]}, y=3),
            table("Longest outstanding", """SELECT f.title AS film,
                         cu.first_name || ' ' || cu.last_name AS customer,
                         r.rental_date::date AS taken_out
                  FROM rental r
                  JOIN inventory i ON i.inventory_id = r.inventory_id
                  JOIN film f ON f.film_id = i.film_id
                  JOIN customer cu ON cu.customer_id = r.customer_id
                  WHERE r.return_date IS NULL
                  ORDER BY r.rental_date ASC LIMIT {{ top }}""", y=9),
        ],
    },
    {
        "title": "Catalogue profile",
        "description": "The shape of the catalogue: length, price and replacement cost.",
        "parameters": [],
        "tiles": [
            metric("Films", "SELECT COUNT(*) FROM film", 0),
            metric("Average minutes", "SELECT ROUND(AVG(length)) FROM film", 3),
            metric("Average rental rate",
                   "SELECT ROUND(AVG(rental_rate)::numeric, 2) FROM film", 6),
            metric("Copies in inventory", "SELECT COUNT(*) FROM inventory", 9),
            chart("Films by length band", """SELECT CASE
                           WHEN length < 60 THEN 'Under 60 min'
                           WHEN length < 90 THEN '60 to 90 min'
                           WHEN length < 120 THEN '90 to 120 min'
                           WHEN length < 150 THEN '120 to 150 min'
                           ELSE '150 min and over' END AS band,
                         COUNT(*) AS films
                  FROM film GROUP BY 1 ORDER BY MIN(length)""",
                  {"type": "bar", "x": "band", "y": ["films"]}, y=3, width=6),
            chart("Rental rates offered", """SELECT rental_rate::text AS rate, COUNT(*) AS films
                  FROM film GROUP BY 1""",
                  {"type": "pie", "x": "rate", "y": ["films"]}, y=3, x=6, width=6),
            table("Category profile", """SELECT c.name AS category, COUNT(*) AS films,
                         ROUND(AVG(f.length)) AS average_minutes,
                         ROUND(AVG(f.rental_rate)::numeric, 2) AS average_rate,
                         ROUND(AVG(f.replacement_cost)::numeric, 2) AS average_replacement
                  FROM film f
                  JOIN film_category fc ON fc.film_id = f.film_id
                  JOIN category c ON c.category_id = fc.category_id
                  GROUP BY 1 ORDER BY films DESC""", y=9),
        ],
    },
    {
        "title": "Rental patterns by weekday",
        "description": "Which days the shop is busy.",
        "parameters": [period(*PAGILA_PERIOD)],
        "tiles": [
            chart("Rentals by weekday", """SELECT TRIM(TO_CHAR(rental_date, 'Day')) AS weekday,
                         COUNT(*) AS rentals
                  FROM rental
                  WHERE rental_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY MIN(EXTRACT(ISODOW FROM rental_date))""",
                  {"type": "bar", "x": "weekday", "y": ["rentals"]}, y=0, width=6),
            chart("Revenue by weekday", """SELECT TRIM(TO_CHAR(p.payment_date, 'Day')) AS weekday,
                         ROUND(SUM(p.amount)::numeric, 2) AS revenue
                  FROM payment p
                  WHERE p.payment_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY MIN(EXTRACT(ISODOW FROM p.payment_date))""",
                  {"type": "bar", "x": "weekday", "y": ["revenue"]}, y=0, x=6, width=6),
            table("Weekday detail", """SELECT TRIM(TO_CHAR(r.rental_date, 'Day')) AS weekday,
                         COUNT(*) AS rentals,
                         COUNT(*) FILTER (WHERE r.return_date IS NULL) AS still_out,
                         COUNT(DISTINCT r.customer_id) AS customers
                  FROM rental r
                  WHERE r.rental_date::date BETWEEN {{ period_start }} AND {{ period_end }}
                  GROUP BY 1 ORDER BY MIN(EXTRACT(ISODOW FROM r.rental_date))""", y=6),
        ],
    },
]


#: Every workspace, and every report defined for it. Ten each: the four banks
#: above were written first and are extended here rather than rewritten, so the
#: reports already seeded keep their titles and nobody's saved link breaks.
BANKS: Dict[str, List[Dict[str, Any]]] = {
    "chinook": CHINOOK + CHINOOK_MORE,
    "demo": DEMO,
    "northwind": NORTHWIND + NORTHWIND_MORE,
    "world": WORLD + WORLD_MORE,
    "healthcare": HEALTHCARE + HEALTHCARE_MORE,
    "ecommerce": ECOMMERCE,
    "employees": EMPLOYEES,
    "booking": BOOKING,
    "pagila": PAGILA,
}


# ----------------------------------------------------------------------
# Posting them
# ----------------------------------------------------------------------


class Client:
    def __init__(self, base: str, tenant: str) -> None:
        self.base = base.rstrip("/")
        self.tenant = tenant
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def _csrf(self) -> str:
        for cookie in self.jar:
            if cookie.name == "vanna_csrf":
                return urllib.parse.unquote(cookie.value or "")
        return ""

    def call(self, method: str, path: str, body: Any = None) -> Tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "X-Tenant-Id": self.tenant}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token := self._csrf():
            headers["X-CSRF-Token"] = token
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=180) as response:
                raw = response.read().decode(errors="replace")
                try:
                    return response.status, json.loads(raw or "{}")
                except json.JSONDecodeError:
                    return response.status, {"detail": raw[:200]}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return exc.code, {"detail": raw[:300]}

    def sign_in(self, email: str, password: str) -> None:
        self.call("GET", "/")
        status, payload = self.call(
            "POST", "/api/vanna/v2/auth/login",
            {"email": email, "password": password, "tenant": self.tenant},
        )
        if status != 200:
            raise SystemExit(f"sign-in failed for {self.tenant} ({status}): {payload}")


def _detail(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload)[:220]
    return str(payload)[:220]


def seed(client: Client, reports: List[Dict[str, Any]], *, check: bool) -> Dict[str, int]:
    counts = {"saved": 0, "failed": 0, "broken_tiles": 0}
    for report in reports:
        status, payload = client.call("POST", "/api/vanna/v2/dashboards", report)
        if status != 200:
            counts["failed"] += 1
            print(f"    FAILED {report['title']!r} ({status}): {_detail(payload)}")
            continue

        counts["saved"] += 1
        stored = payload["dashboard"]
        warnings = payload.get("warnings") or []
        line = f"    {report['title']}"
        if warnings:
            line += f"  [warnings: {len(warnings)}]"
        print(line)

        if not check:
            continue
        # Render once with the defaults. A report that stores fine and dies on read is
        # the failure this whole tool exists to avoid handing to somebody.
        code, data = client.call(
            "GET", f"/api/vanna/v2/dashboards/{stored['id']}/data"
        )
        if code != 200:
            counts["broken_tiles"] += 1
            print(f"      does not render ({code}): {_detail(data)}")
            continue
        broken = [
            r for r in (data.get("results") or []) if r.get("error")
        ]
        if broken:
            counts["broken_tiles"] += len(broken)
            for bad in broken[:3]:
                print(f"      tile error: {bad['error'][:150]}")
    return counts


def clean(client: Client, reports: List[Dict[str, Any]]) -> None:
    titles = {report["title"] for report in reports}
    status, payload = client.call("GET", "/api/vanna/v2/dashboards")
    for row in (payload.get("dashboards") or []) if status == 200 else []:
        document = row.get("document") or row
        if document.get("title") in titles:
            client.call("DELETE", f"/api/vanna/v2/dashboards/{document['id']}")
    print("    previous copies removed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--tenant", default="all",
        help=f"a workspace, a comma-separated list, or 'all'. Known: {', '.join(BANKS)}",
    )
    parser.add_argument("--clean", action="store_true", help="remove previous copies first")
    parser.add_argument(
        "--no-check", action="store_true",
        help="skip the render check (faster, and tells you less)",
    )
    args = parser.parse_args()

    if args.tenant == "all":
        wanted = list(BANKS)
    else:
        wanted = [t.strip() for t in args.tenant.split(",") if t.strip()]
        if unknown := [t for t in wanted if t not in BANKS]:
            raise SystemExit(f"no reports defined for {', '.join(unknown)}")

    total = {"saved": 0, "failed": 0, "broken_tiles": 0}
    planned = sum(len(BANKS[t]) for t in wanted)
    print(f"seeding {planned} reports across {len(wanted)} workspace(s)\n")

    for tenant in wanted:
        print(f"  {tenant}")
        client = Client(args.url, tenant)
        client.sign_in(args.email, args.password)
        if args.clean:
            clean(client, BANKS[tenant])
        counts = seed(client, BANKS[tenant], check=not args.no_check)
        for key in total:
            total[key] += counts[key]

    print(f"\n--- result ---\n  saved {total['saved']}/{planned}")
    if total["failed"]:
        print(f"  refused        : {total['failed']}")
    if total["broken_tiles"]:
        print(f"  tiles in error : {total['broken_tiles']}")
    return 1 if total["failed"] or total["broken_tiles"] else 0


if __name__ == "__main__":
    sys.exit(main())
