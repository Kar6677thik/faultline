FROM faultline-runtime:0.1.0
COPY src/faultline_counter /app/faultline_counter
ENV PYTHONPATH=/app
CMD ["uvicorn", "faultline_counter.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
