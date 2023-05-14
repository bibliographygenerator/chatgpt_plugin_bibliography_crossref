import json
from fastapi import FastAPI, Request, HTTPException
import httpx
import warnings
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse
import requests
from bibtexparser.bwriter import BibTexWriter
from bibtexparser.bibdatabase import BibDatabase
import bibtexparser
from typing import List, Dict
import asyncio
from pyzotero import zotero
import uvicorn
import os
from urllib.parse import quote

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(debug=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"]
)

@app.middleware("http")
async def add_cors_header(request: Request, call_next):
    response = await call_next(request)
    allowed_origin = request.headers.get("Origin")
    response.headers["Access-Control-Allow-Origin"] = allowed_origin or "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.route("/.well-known/ai-plugin.json", methods=["GET", "OPTIONS"])
async def options_handler(request: Request):
    if request.method == "GET":
        try:
            return FileResponse("./.well-known/ai-plugin.json")
        except FileNotFoundError:
            response = JSONResponse(content={"error": "File not found"}, status_code=404)
            response.headers["Access-Control-Allow-Origin"] = request.headers["Host"]
            return response
    elif request.method == "OPTIONS":
        try:
            with open("./.well-known/ai-plugin.json") as f:
                text = f.read()
                response = JSONResponse(content=text, media_type="text/json")
                response.headers["Access-Control-Allow-Origin"] = request.headers["Host"]
                return response
        except FileNotFoundError:
            return JSONResponse(content={"error": "File not found"}, status_code=404)

def sanitize(data):
    if isinstance(data, str):
        return data.encode('charmap', 'ignore').decode('charmap')
    elif isinstance(data, dict):
        return {key: sanitize(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [sanitize(element) for element in data]
    else:
        return data

@app.get("/freetext_to_crossref_items/")
async def freetext_to_crossref_items(search_term: str):
    """
    Args:
    search_term (str): The query string to search for via Crossref API.

    Returns:
    list: A list of bibliography items related to the search query.
    """
    try:
        response = requests.get(
            f"https://api.crossref.org/works?rows=10&sort=relevance&query={quote(search_term)}", 
            headers={"User-Agent": "ChatGPT Plugin Bibliography/1.0 (+https://bibliography.pythonanywhere.com/static/legal.html; mailto:bibliography_generator@proton.me)"
            }
        )
        
        return response.json()
        
        response_json = sanitize(response.json())
        
        if response.status_code != 200 or not response_json['message']['items']:
            return ['fetching content failed']
        
        crossref_items = [ response_json['message']['items'] ] if response.status_code == 200 else [] 
        return crossref_items
    except Exception as e:
        print(e)
        return [f'Error contacting Crossref: {e}']

@app.get("/crossref_items_to_dois/")
async def crossref_items_to_dois(search_term: str):
    try:
        crossref_items = freetext_to_crossref_items(search_term)
        return JSONResponse(content=[ item['DOI'] for item in crossref_items ], status_code=200)
    except Exception as e:
        print(e)
        return JSONResponse(
            content={'error': 'conversion to DOIs failed', 'crossref_items': crossref_items}, 
            status_code=199
        )

@app.get("/crossref_items_to_bibtex/")
async def crossref_items_to_bibtex(crossref_items):
    """
    Return a bibtex string of metadata for a given DOI.

    Args:
        crossref_items (list): The list of scholarly articles in crossref API response format.
    
    Returns:
        str: A BibTex citation for the scholarly article. If there is an error (e.g., the DOI does not exist), it returns an empty string.
    """
    try:
        bibtex_entries = []

        for item in crossref_items:
            try:
                bibtex_entries.append(requests.get(
                    f"http://api.crossref.org/works/{item.get('DOI')}/transform/application/x-bibtex")
                ).json()
            except Exception as e:
                try:
                    bibtex_item = {
                        'ENTRYTYPE': item.get('type', ''),
                        'ID': item.get('DOI', ''),
                        'publisher': item.get('publisher', ''),
                        'year': str(item.get('created', {}).get('date-parts', [[None]])[0][0]) if item.get('created', {}).get('date-parts') else '',
                        'doi': item.get('DOI', ''),
                        'title': item.get('title', [''])[0],
                        'journal': item.get('short-container-title', [''])[0],
                        'author': ' and '.join([f"{author.get('given', '')} {author.get('family', '')}" for author in item.get('author', [])]),
                    }
                    
                    db = BibDatabase()
                    db.entries = [bibtex_item]
        
                    writer = BibTexWriter()
                    
                    bibtex_str = writer.write(db)
        
                    bibtex_str = bibtex_item
        
                    bibtex_entries.append(bibtex_str)
                except Exception as e:
                    print(f"Failed to convert item: {e}")

        return bibtex_entries

    except Exception as e:
        print(f"Conversion of crossref items to BibTex failed, Error: {e}")
        return crossref_items


@app.get("/freetext_to_bibtex/")
async def freetext_to_bibtex(search_term: str):
    try:        
        crossref_items = await freetext_to_crossref_items(search_term)
        
        if not crossref_items:
            warnings.warn(f"No items found for {search_term}")

        bibtex_list = await crossref_items_to_bibtex(crossref_items)
        
        if not bibtex_list:
            warnings.warn(f"fetching result failed for input {search_term=}, {crossref_items=}")
        return JSONResponse(content={'bibtex': bibtex_list}, status_code=200)
    except Exception as e:
        return {'error': f"Error fetching result for input '{search_term}': {str(e)}"}
        

@app.post("/add_bibtex_to_zotero/")
async def add_bibtex_to_zotero(request: Request):
    """
    Endpoint to add a BibTeX item to a Zotero collection.

    Args:
    request (Request): FastAPI request object.

    Returns:
    dict: A status message.
    """
    data = await request.json()

    try:
        api_key = data["api_key"]
        library_id = data["library_id"]
        collection_id = data["collection_id"]
        bibtex = data["bibtex"]
        user = data["user"]
    except KeyError as e:
        warnings.warn(f"Missing required parameter: {e.args[0]}")

    zot = zotero.Zotero(library_id, user, api_key)

    try:
        db = bibtexparser.loads(bibtex)
        bibtex_dict = db.entries_dict
    except Exception as e:
        warnings.warn(f"Error parsing BibTeX data: {str(e)}")

    try:
        zot.add_items(bibtex_dict, collection=collection_id)
    except Exception as e:
        warnings.warn(f"Error adding items to Zotero: {str(e)}")

    return {"message": "BibTex added to Zotero collection successfully!"}

@app.get("/openapi.yaml")
async def openapi_spec():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Bibliography Generator",
        version="1.0",
        description="Democratizing access to scientific research with natural language using Crossref and Zotero",
        routes=app.routes,
    )
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = openapi_spec

if __name__ == '__main__':
    os.system("uvicorn main:app --host http://bibliography.pythonanywhere.com --port 8080 --reload")