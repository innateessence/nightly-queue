from django import forms

from .models import Project


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "repository_url", "base_branch", "credential", "autonomous"]
        labels = {"repository_url": "GitHub repository URL"}
        help_texts = {
            "repository_url": "Use an HTTPS URL, such as https://github.com/owner/repo.",
            "base_branch": "Leave blank to use the repository's default branch.",
            "credential": "Choose a saved credential. Manage credentials on the Credentials page.",
            "autonomous": "Import open issues and make imported issues eligible for claiming. Manual tasks remain independently claimable.",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Nightly queue"}),
            "repository_url": forms.URLInput(
                attrs={"placeholder": "https://github.com/owner/repo"}
            ),
            "base_branch": forms.TextInput(attrs={"placeholder": "Repository default"}),
            "autonomous": forms.CheckboxInput(
                attrs={
                    "aria-labelledby": "id_autonomous_label",
                    "aria-describedby": "id_autonomous_helptext",
                }
            ),
        }
