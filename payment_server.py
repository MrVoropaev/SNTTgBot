from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/yookassa/webhook")
async def webhook(request: Request):

    data = await request.json()

    event = data.get("event")

    if event == "payment.succeeded":

        payment = data["object"]

        amount = payment["amount"]["value"]

        description = payment["description"]

        print("Оплата прошла")
        print(amount)
        print(description)

    return {"status": "ok"}