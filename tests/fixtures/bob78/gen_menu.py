from reportlab.lib.pagesizes import A5
from reportlab.pdfgen import canvas
c = canvas.Canvas("/home/zarkone/any/anybao/tests/fixtures/bob78/menu.pdf", pagesize=A5)
c.setFont("Helvetica-Bold", 18); c.drawString(60, 540, "Supra House — Tasting Menu")
c.setFont("Helvetica-Oblique", 11); c.drawString(60, 520, "Chef's five courses, autumn 2026")
y = 480
for title, desc in [
  ("I. Pkhali trio", "beet, spinach and bean pastes with walnut and pomegranate"),
  ("II. Khinkali duo", "beef-pork and mushroom dumplings, cracked black pepper"),
  ("III. Trout on the coals", "lake trout, tarragon butter, grilled lemon"),
  ("IV. Mtsvadi", "pork skewer over vine embers, plum tkemali sauce"),
  ("V. Churchkhela & matsoni", "walnut candle candy, honey-lavender yogurt"),
]:
    c.setFont("Helvetica-Bold", 13); c.drawString(60, y, title)
    c.setFont("Helvetica", 10); c.drawString(72, y-16, desc); y -= 52
c.setFont("Helvetica-Oblique", 9); c.drawString(60, y-10, "Wine pairing available on request. Service 10%.")
c.save(); print("PDF-OK")
