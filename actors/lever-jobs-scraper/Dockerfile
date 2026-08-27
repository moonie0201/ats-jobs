FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN echo "Python: $(python --version)" \
 && pip install --no-cache-dir -r requirements.txt \
 && pip freeze

COPY core ./core
COPY src ./src

RUN python -c "import core, src.main" \
 && python -m compileall -q core src

CMD ["python", "-m", "src.main"]
