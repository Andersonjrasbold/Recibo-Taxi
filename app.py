from flask import Flask, render_template, request, redirect, url_for, abort
from uuid import uuid4
from datetime import datetime

app = Flask(__name__)

# "banco" em memória para demo
RECIBOS = {}

# rota de saúde para o Fly
@app.route("/health")
def health():
    return "OK", 200

@app.route("/")
def index():
    return render_template("index.html")

@app.post("/recibo")
def recibo_criar():
    form = request.form
    rid = uuid4().hex[:8].upper()

    dados = {
        "rid": rid,
        "passageiro": form.get("passageiro", "").strip(),
        "data": form.get("data", ""),
        "origem": form.get("origem", "").strip(),
        "destino": form.get("destino", "").strip(),
        "valor": form.get("valor", "").strip(),
        "forma_pagamento": form.get("forma_pagamento", "").strip(),
        "placa": form.get("placa", "").strip(),
        "motorista": form.get("motorista", "").strip(),
        "observacoes": form.get("observacoes", "").strip(),
        "created_at": datetime.utcnow().isoformat()
    }
    RECIBOS[rid] = dados
    return redirect(url_for("recibo_view", rid=rid))

@app.get("/recibo/<rid>")
def recibo_view(rid):
    dados = RECIBOS.get(rid)
    if not dados:
        abort(404, description="Recibo não encontrado")
    return render_template("recibo_view.html", dados=dados)

if __name__ == "__main__":
    app.run(debug=True)
