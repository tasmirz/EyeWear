# %%
from dotenv import load_dotenv
load_dotenv()
import os
from cerebras.cloud.sdk import Cerebras
# %%
client = Cerebras(
    # This is the default and can be omitted
    api_key=os.environ.get("CEREBRAS_API_KEY")
)

def qwen_cerbas_ocr_html_extraction_api_call(system_prompt,user_prompt ):
    client = Cerebras(
        # This is the default and can be omitted
        api_key=os.environ.get("CEREBRAS_API_KEY")
    )
    res = client.chat.completions.create(
    messages=[
        {
            "role": "system",
            "content": system_prompt
        },
        {
            "role": "user",
            "content": user_prompt
        }
    ],
    model="qwen-3-235b-a22b-instruct-2507",
    stream=False,
    max_completion_tokens=20000,
    temperature=0.7,
    top_p=0.8
    )
    print(res)
    return res.choices[0].message['content']

q= qwen_cerbas_ocr_html_extraction_api_call('yay','nay')
# %%
