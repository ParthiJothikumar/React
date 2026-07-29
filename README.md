{
    "name": "issue_classification",
    "strict": true,
    "schema": {
      "type": "object",
      "properties": {
        "issue_type": {
          "type": "string",
          "enum": ["greeting", "non_it", "non_outlook_it", "outlook_it"]
        }
      },
      "required": ["issue_type"],
      "additionalProperties": false
    }
  }
