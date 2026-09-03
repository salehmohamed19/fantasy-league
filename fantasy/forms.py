from django import forms
from django.contrib.auth.models import User
from .models import UserProfile, UserFantasyTeam

class UserUpdateForm(forms.ModelForm):
    email = forms.EmailField(required=False, widget=forms.EmailInput(attrs={'class': 'w-full bg-slate-900 text-white rounded-lg p-2.5 border border-gray-700 text-sm focus:border-plGreen outline-none'}))
    first_name = forms.CharField(max_length=30, required=False, widget=forms.TextInput(attrs={'class': 'w-full bg-slate-900 text-white rounded-lg p-2.5 border border-gray-700 text-sm focus:border-plGreen outline-none'}))
    last_name = forms.CharField(max_length=30, required=False, widget=forms.TextInput(attrs={'class': 'w-full bg-slate-900 text-white rounded-lg p-2.5 border border-gray-700 text-sm focus:border-plGreen outline-none'}))

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']

class ProfileUpdateForm(forms.ModelForm):
    avatar = forms.ImageField(required=False, widget=forms.FileInput(attrs={'class': 'hidden', 'id': 'avatar-input', 'accept': 'image/*'}))

    class Meta:
        model = UserProfile
        fields = ['avatar']

class TeamNameUpdateForm(forms.ModelForm):
    name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'w-full bg-slate-900 text-white rounded-lg p-2 border border-gray-700 text-sm focus:border-plGreen outline-none'}))

    class Meta:
        model = UserFantasyTeam
        fields = ['name']