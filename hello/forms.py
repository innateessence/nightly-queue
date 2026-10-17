from django import forms

from .models import QueueItem


class QueueItemForm(forms.ModelForm):
    class Meta:
        model = QueueItem
        fields = ["title", "instructions", "priority"]
        widgets = {
            "title": forms.TextInput(
                attrs={"placeholder": "e.g. Research competitors for the new launch"}
            ),
            "instructions": forms.Textarea(
                attrs={
                    "rows": 5,
                    "placeholder": "Add context, links, and what a good result looks like…",
                }
            ),
        }
