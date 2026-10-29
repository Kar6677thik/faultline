# The transfer application ships inside the Faultline runtime; only the command differs.
FROM faultline-runtime:0.1.0
CMD ["python", "-m", "uvicorn", "faultline.transfers:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
