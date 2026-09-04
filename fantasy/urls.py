from django.urls import path
from django.contrib.auth.views import LoginView, LogoutView
from . import views

urlpatterns = [
    # الرئيسية وبناء التشكيلة
    path('', views.squad_builder, name='squad_builder'),
    path('squad-builder/', views.squad_builder, name='squad_builder'),
    path('save-squad/', views.save_squad, name='save_squad'),
    path('add-player/<int:player_id>/', views.add_player_to_squad, name='add_player_to_squad'),
    path('remove-player/<int:player_id>/', views.remove_player_from_squad, name='remove_player'),
    path('set-captain/<int:player_id>/', views.set_captain, name='set_captain'),
    path('set-vice-captain/<int:player_id>/', views.set_vice_captain, name='set_vice_captain'),
    path('swap/<int:starter_id>/<int:sub_id>/', views.swap_players, name='swap_players'),
    path('move-to-starter/<int:player_id>/', views.move_to_starter, name='move_to_starter'),

    # البطولات والترتيب
    path('leagues/', views.leagues_hub, name='leagues_hub'),
    path('leagues/join/<int:league_id>/', views.join_league, name='join_league'),
    path('leagues/join-by-code/', views.join_league, name='join_private_league'),
    path('leaderboard/', views.leaderboard, name='leaderboard'),

    # الحساب الشخصي والسجل
    path('profile/', views.profile_view, name='profile'),
    path('history/', views.gameweeks_history_view, name='leagues_history'),
    path('history/squad/<int:gw_id>/', views.view_closed_squad, name='view_closed_squad'),

    # لوحة تحكم الأدمن للتحكم بالجولات
    path('admin-panel/close-gameweek/', views.close_current_gameweek, name='close_current_gameweek'),
    path('admin-panel/open-next-gameweek/', views.open_next_gameweek, name='open_next_gameweek'),
    path('admin-panel/close-and-advance/', views.close_and_advance_gameweek, name='close_and_advance_gameweek'),
    path('api/get-player-previous-yellows/', views.get_player_previous_yellow_cards, name='get_player_previous_yellow_cards'),
    path('enter-stats/', views.enter_match_stats, name='enter_match_stats'),
    
    # المصادقة والحسابات
    path('login/', LoginView.as_view(template_name='fantasy/login.html', redirect_authenticated_user=True), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('register/', views.register, name='register'),
    path('history/', views.gameweeks_history_view, name='gameweeks_history'),
    path('gameweek/<int:gw_id>/publish/', views.publish_gameweek_stats, name='publish_gameweek_stats'),
]