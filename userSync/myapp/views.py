from django.http import HttpResponse


def index(request):
    return HttpResponse("PeopleStrong to Microsoft sync app is running.")
