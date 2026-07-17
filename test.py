from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

kv = SecretClient(
    vault_url="https://<your-vault>.vault.azure.net/",
    credential=DefaultAzureCredential(),
)

try:
    s = kv.get_secret("orchestrator-agent-key")
    print("✅ found:", s.name, "| value length:", len(s.value))
except Exception as e:
    print("❌ could not read:", e)
