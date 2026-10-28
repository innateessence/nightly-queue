from django import forms

from .models import Project


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "repository_url", "base_branch"]
        labels = {"repository_url": "GitHub repository URL"}
        help_texts = {
            "repository_url": "Use an HTTPS URL, such as https://github.com/owner/repo.",
            "base_branch": "Leave blank to use the repository's default branch.",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Nightly queue"}),
            "repository_url": forms.URLInput(
                attrs={"placeholder": "https://github.com/owner/repo"}
            ),
            "base_branch": forms.TextInput(attrs={"placeholder": "Repository default"}),
        }
