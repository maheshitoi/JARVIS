
import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY")

if not api_key:
    print("API Key not found")
else:
    genai.configure(api_key=api_key)
    try:
        models = genai.list_models()
        with open("bidi_model_name.txt", "w") as f:
            for m in models:
                if 'bidiGenerateContent' in m.supported_generation_methods:
                    f.write(m.name + "\n")
    except Exception as e:
        print(f"Error: {e}")
