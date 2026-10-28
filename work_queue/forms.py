from django import forms

from .models import QueueItem
from .services import enqueue_item


class QueueItemForm(forms.ModelForm):
    class Meta:
        model = QueueItem
        fields = ["project", "title", "instructions", "priority"]
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

    def save(self, commit=True):
        if not commit:
            return super().save(commit=False)
        if self.errors:
            raise ValueError("Cannot save an invalid queue item.")
        return enqueue_item(**self.cleaned_data)
