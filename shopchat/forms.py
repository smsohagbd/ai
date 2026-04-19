from django import forms

from shopchat.models import AppSettings, GeminiApiCredential, ProductImage


class AppSettingsForm(forms.ModelForm):
    class Meta:
        model = AppSettings
        fields = [
            "deployment_mode",
            "messenger_verify_token",
            "system_prompt",
            "embedding_output_dimensionality",
            "similarity_top_k",
        ]
        widgets = {
            "system_prompt": forms.Textarea(attrs={"rows": 12, "class": "field-textarea"}),
            "deployment_mode": forms.RadioSelect(
                attrs={"class": "deploy-radio-input"}
            ),
            "messenger_verify_token": forms.TextInput(
                attrs={
                    "class": "field-text",
                    "autocomplete": "off",
                    "placeholder": "e.g. my-secret-verify-string-2026",
                }
            ),
        }
        labels = {
            "deployment_mode": "Messenger mode",
            "messenger_verify_token": "Meta webhook verify token (yours)",
        }


class GeminiApiCredentialForm(forms.ModelForm):
    class Meta:
        model = GeminiApiCredential
        fields = ["label", "api_key", "enabled", "sort_order"]
        widgets = {
            "api_key": forms.PasswordInput(
                render_value=True,
                attrs={"autocomplete": "off", "placeholder": "AIza…"},
            ),
            "label": forms.TextInput(attrs={"placeholder": "e.g. Personal key1"}),
        }


class ProductImageForm(forms.ModelForm):
    class Meta:
        model = ProductImage
        fields = ["name", "image", "notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
