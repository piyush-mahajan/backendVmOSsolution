for checking url json 
curl -X POST http://localhost:3000/api/process-transcription \
-H "Content-Type: application/json" \
-d '{
  "transcription_url": ".txt file "
}'

