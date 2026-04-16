# Lambda Container Image for IODP Agent
# Base: AWS Lambda Python 3.12 runtime (includes boto3)
FROM public.ecr.aws/lambda/python:3.12

# Install dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Copy application code
COPY src/ ${LAMBDA_TASK_ROOT}/src/

# Copy Lambda handler entry point
COPY lambda_handler.py ${LAMBDA_TASK_ROOT}/

# Mangum adapts FastAPI to Lambda event format
CMD ["lambda_handler.handler"]
