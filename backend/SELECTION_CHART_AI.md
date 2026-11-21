# Selection Chart AI Interpreter

The selection-chart learning workflow optionally uses the OpenAI Responses API.

## Configuration

Set these backend environment variables (Render environment, never commit secrets):

- `OPENAI_API_KEY`: required to enable multimodal interpretation.
- `SELECTION_CHART_AI_MODEL`: optional; defaults to `gpt-4.1`.

Without the API key, the workflow falls back to a conservative text-assisted draft. The fallback is not visual AI interpretation.

## Input behavior

- PDF: original PDF and extracted text are sent to the model with high visual detail. PDF pages can contain chart layouts, tables, and diagrams.
- XLSX: extracted workbook rows/text are sent as text. Embedded Excel images/charts are not interpreted in this initial implementation.

## Safety

AI output is a draft. Admin review, mapping approval, test bench execution, and separate production approval are required. AI output does not alter the production BOM or master database automatically.

## Cost and privacy

PDF page images and extracted text may consume significant API tokens. Only selection-chart documents should be sent. Do not upload unrelated confidential documents. Review provider retention/data settings and organizational policy before enabling in production.
