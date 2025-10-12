"""
Connection 
1. There is priv, publ keys in ./keys
2. request challenge from server, sign with priv key, send back
3. server verifies with publ key
4. Receive JWT token, server publ key



1. Receive image filename (message queue)
2. Send image to OCR server, encrypt with server publ key
3. Receive playable text (pipe to tts_pipeline.py), decrypt with server publ key
4. Ensure piped sequentially
"""
