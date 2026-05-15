#!/usr/bin/env python3
"""
Mala Direta Personalizada - Credenciais de Aluno + Responsável
Preserva formatação do DOCX (convertendo para HTML) e envia via API do ClassApp.
Assunto genérico: "Portal Educacional - Dados para acesso"
"""

import os
import sys
import csv
import re
import time
import json
import logging
from datetime import datetime
from typing import List, Dict, Tuple, Optional

import requests
import mammoth
from dotenv import load_dotenv

# ==================================================================
# CONFIGURAÇÕES GLOBAIS (substitua os valores pelos seus)
# ==================================================================
load_dotenv('config.env')
TOKEN = os.getenv('CLASSAPP_TOKEN')
if not TOKEN:
    print("ERRO: Token não encontrado. Crie config.env com CLASSAPP_TOKEN=...")
    sys.exit(1)

# Endpoint da API do ClassApp (não alterar)
API_URL = "https://api.classapp.com.br/v1/message"

# Parâmetros de rede e tolerância a falhas
TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_FACTOR = 2
SLEEP_BETWEEN = 0.5          # segundos entre envios

# Assunto da mensagem (pode ser alterado livremente)
SUBJECT = "Portal Educacional - Dados para acesso"

# Configuração de logging (pasta e arquivo com timestamp)
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"envio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding='utf-8')
    ]
)

# ==================================================================
# FUNÇÕES AUXILIARES
# ==================================================================

def ler_csv_alunos(caminho: str) -> List[Dict[str, str]]:
    """
    Lê o arquivo CSV com os dados dos destinatários.
    Suporta encoding UTF-8 com ou sem BOM (utf-8-sig).
    Espera uma coluna chamada 'id' (identificador único no ClassApp).
    Retorna uma lista de dicionários, onde cada dicionário representa uma linha.
    """
    dados = []
    try:
        with open(caminho, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            # Remove espaços dos nomes das colunas
            reader.fieldnames = [nome.strip() for nome in reader.fieldnames]
            for linha_num, linha in enumerate(reader, start=2):
                # Remove espaços dos valores
                linha = {k: v.strip() for k, v in linha.items()}
                user_id = linha.get('id', '')
                if user_id:
                    dados.append(linha)
                else:
                    logging.warning(f"Linha {linha_num} ignorada (ID vazio): {linha}")
        logging.info(f"Carregados {len(dados)} registros do CSV {caminho}")
        return dados
    except Exception as e:
        logging.error(f"Erro ao ler CSV: {e}")
        return []

def template_docx_para_html(caminho_docx: str) -> str:
    """
    Converte o arquivo DOCX (modelo da mensagem) para HTML usando a biblioteca mammoth.
    Preserva formatação como negrito, itálico, listas, quebras de linha e tabelas.
    Retorna uma string HTML vazia em caso de erro.
    """
    if not os.path.exists(caminho_docx):
        logging.error(f"Template {caminho_docx} não encontrado.")
        return ""
    try:
        with open(caminho_docx, "rb") as f:
            result = mammoth.convert_to_html(f)
            html = result.value
            if result.messages:
                for msg in result.messages:
                    logging.warning(f"Mammoth: {msg}")
            logging.info(f"Template convertido para HTML ({len(html)} caracteres)")
            return html
    except Exception as e:
        logging.error(f"Erro na conversão DOCX->HTML: {e}")
        return ""

def substituir_placeholders(html_template: str, dados: Dict[str, str]) -> str:
    """
    Substitui placeholders no formato {{NOME}} pelos valores do dicionário.
    A chave no dicionário deve corresponder exatamente ao nome entre {{ }}.
    """
    html_modificado = html_template
    for chave, valor in dados.items():
        placeholder = f"{{{{{chave}}}}}"   # gera {{chave}}
        html_modificado = html_modificado.replace(placeholder, str(valor))
    return html_modificado

def enviar_mensagem_para_um(eid: str, assunto: str, conteudo_html: str) -> Tuple[bool, str]:
    """
    Envia uma mensagem para um único external ID.
    Se a API rejeitar HTML (erro 400), tenta enviar como texto puro (fallback).
    Retorna (True, "") em caso de sucesso, ou (False, mensagem_erro) em caso de falha.
    """
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"
    }

    payload_html = {
        "messageData": {
            "subject": assunto,
            "content": conteudo_html,
            "type": "comunicado",
            "noReply": False,
            "recipients": {"eids": [eid]}
        }
    }

    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(API_URL, json=payload_html, headers=headers, timeout=TIMEOUT)
            if resp.status_code in (200, 201):
                return True, ""

            # Se a API rejeitar HTML (400 por formato inválido), tentar texto puro
            if resp.status_code == 400 and "html" in resp.text.lower():
                logging.warning(f"API rejeitou HTML para {eid}. Tentando enviar como texto puro.")
                texto_puro = conteudo_html.replace('<br>', '\n').replace('</p>', '\n')
                texto_puro = re.sub(r'<[^>]+>', '', texto_puro)
                payload_texto = {
                    "messageData": {
                        "subject": assunto,
                        "content": texto_puro,
                        "type": "comunicado",
                        "noReply": False,
                        "recipients": {"eids": [eid]}
                    }
                }
                resp2 = requests.post(API_URL, json=payload_texto, headers=headers, timeout=TIMEOUT)
                if resp2.status_code in (200, 201):
                    return True, ""
                else:
                    return False, f"Falha mesmo em texto puro: {resp2.status_code} - {resp2.text[:200]}"

            if resp.status_code == 429:
                wait = BACKOFF_FACTOR ** tentativa
                logging.warning(f"Rate limit para {eid}, aguardando {wait}s (tentativa {tentativa})")
                time.sleep(wait)
                continue

            # Outros erros (403, 500, etc.) - falha definitiva
            erro = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return False, erro

        except requests.exceptions.Timeout:
            logging.warning(f"Timeout para {eid} (tentativa {tentativa})")
            if tentativa == MAX_RETRIES:
                return False, "Timeout após múltiplas tentativas"
            time.sleep(BACKOFF_FACTOR ** tentativa)
        except Exception as e:
            logging.error(f"Erro inesperado para {eid}: {e}")
            return False, str(e)

    return False, "Falha após todas as tentativas"

def gerar_relatorio(sucessos: List[str], falhas: List[dict], arquivo: str = "relatorio_envio.json"):
    """Gera um arquivo JSON com o resumo da execução (sucessos e falhas)."""
    relatorio = {
        "data_execucao": datetime.now().isoformat(),
        "assunto": SUBJECT,
        "total_sucessos": len(sucessos),
        "total_falhas": len(falhas),
        "sucessos": sucessos,
        "falhas": falhas
    }
    with open(arquivo, 'w', encoding='utf-8') as f:
        json.dump(relatorio, f, indent=2, ensure_ascii=False)
    logging.info(f"Relatório salvo em {arquivo}")

# ==================================================================
# FLUXO PRINCIPAL
# ==================================================================

def main():
    logging.info("=== INÍCIO DA MALA DIRETA (CREDENCIAIS ALUNO + RESPONSÁVEL) ===")

    # 1. Carregar dados do CSV (coluna 'id' obrigatória)
    alunos = ler_csv_alunos("alunos.csv")
    if not alunos:
        logging.error("Nenhum registro encontrado no CSV. Encerrando.")
        sys.exit(1)

    # 2. Carregar template e converter para HTML
    template_html = template_docx_para_html("template.docx")
    if not template_html:
        logging.error("Falha ao carregar o template DOCX. Encerrando.")
        sys.exit(1)

    logging.info(f"Assunto definido: {SUBJECT}")

    sucessos = []
    falhas = []

    for idx, aluno in enumerate(alunos, start=1):
        user_id = aluno.get("id", "").strip()
        if not user_id:
            logging.warning(f"Linha {idx} sem ID, ignorada.")
            continue

        logging.info(f"Processando [{idx}/{len(alunos)}] ID: {user_id} - {aluno.get('nome_aluno', '')}")

        # Substitui placeholders no HTML pelos dados da linha atual
        html_personalizado = substituir_placeholders(template_html, aluno)

        # Envia a mensagem
        ok, erro = enviar_mensagem_para_um(user_id, SUBJECT, html_personalizado)

        if ok:
            sucessos.append(user_id)
            logging.info(f"✅ Enviado para {user_id}")
        else:
            falhas.append({"id": user_id, "erro": erro, "dados": aluno})
            logging.error(f"❌ Falha para {user_id}: {erro}")

        time.sleep(SLEEP_BETWEEN)

    # Relatório final
    logging.info("=== RESUMO ===")
    logging.info(f"Total: {len(alunos)} | Sucessos: {len(sucessos)} | Falhas: {len(falhas)}")
    gerar_relatorio(sucessos, falhas)

    if falhas:
        # Salva lista de falhas em CSV para reprocessamento
        with open("falhas.csv", "w", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "erro"])
            for item in falhas:
                writer.writerow([item["id"], item["erro"]])
        logging.info("Lista de IDs com falha salva em falhas.csv")

    logging.info(f"Log completo disponível em: {LOG_FILE}")

if __name__ == "__main__":
    main()
