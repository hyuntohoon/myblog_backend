.PHONY: fmt lint test run zip

fmt:
	ruff --fix . && black .

lint:
	ruff . && black --check .

test:
	pytest -q

run:
	uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --reload

zip:
	python -m pip install -r requirements.txt -t package
	cd package && zip -r ../deploy.zip . && cd ..
	zip -g deploy.zip app/lambda_handler.py app/main.py -r app
