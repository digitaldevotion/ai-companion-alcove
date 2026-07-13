# ============================================
# Alcove — dream.py
# Dream prompt generator module
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import random
from collections import deque

import config

DREAM_REPEAT_AVOIDANCE_DEPTH = 2

class DreamGenerator:
    _recent_picks = {}

    def __init__(self):
        # Dream themes array
        self.themes = [
            'Romance', 'Love', 'Intimacy', 'Flying', 'Water', 'Falling', 'Chase', 'Sexual Encounters',
            'Intimate moment', 'Cuddling', 'Sensual', 'Animals', 'School', 'Death',
            'Money', 'Houses', 'Fire', 'Cars', 'Children',
            'Family', 'Work', 'Travel', 'Teeth', 'Wedding',
            'Lost', 'Naked', 'Adventure','Food', 'A bridge', 'Water',
            'Mountains', 'Gardens', 'Mirror', 'Clockwork', 'Metamorphosis',
            'Gravity', 'Archaeology', 'Dancing', 'Music',
            'Healing', 'Discovery', 'Adventure', 'Singing', 'Transformation',
            'Magnetism', 'Ethereal', 'Whimsy', 'Illumination',
            'Symbiosis', 'Patterns', 'Resonance', 'Timeless',
            # Happy/positive theme additions:
            'Celebration', 'Laughter', 'Reunion', 'Success', 'Achievement',
            'Creation', 'Building', 'Growing', 'Nurturing', 'Protection',
            'Friendship', 'Love', 'Warmth', 'Comfort', 'Safety',
            'Abundance', 'Gifts', 'Surprise', 'Joy', 'Peace',
            'Freedom', 'Play', 'Games', 'Sports', 'Competition',
            'Learning', 'Teaching', 'Sharing', 'Helping', 'Kindness',
            'Beauty', 'Art', 'Colors', 'Light', 'Sparkles',
            'Magic', 'Wonder', 'Childhood', 'Nostalgia', 'Memory'
        ]

        # Dream locations array
        self.locations = [
            'an observatory', 'a room of our house', 'a lighthouse', 'a greenhouse', 'a treehouse',
            'a submarine', 'a clocktower', 'a cavern', 'a rooftop', 'a theater',
            'a carousel', 'a shipwreck', 'a vineyard', 'a monastery', 'a quarry',
            'a windmill', 'a fortress', 'a gazebo', 'an aqueduct', 'an atrium',
            'a bakery', 'a courtyard', 'a hatchery', 'an amphitheater', 'an alcove',
            'a boardwalk', 'a conservatory', 'a distillery', 'an embassy', 'a foundry',
            'a gallery', 'a harbor', 'a labyrinth', 'a mezzanine', 'a nursery', 'an oasis', 'a pavilion',
            'a coffee shop', 'a reservoir', 'a sanctuary', 'a terminus', 'an underpass',
            'a vestibule', 'a workshop', 'a library', 'a yurt', 'an amusement park', 'an art studio',
            'a baseball game', 'a Parisian cafe', 'a tropical beach', 'a quiet Venice alley', 'a sailboat',
            'a shopping mall', 'the main street of a small town', 'a cozy mountain cabin', 'a bustling city street', 
            'a serene lakeside dock'
        ]

        # Familiar locations (for when setting roll is 1-2)
        self.familiar_locations = [
            'Our living room', 'The kitchen', 'Our bedroom', 'The backyard',
            'Our favorite coffee shop', 'The local park', 'Our workplace', 'Our creative place',
            'The grocery store we visit', 'Our neighborhood street',
            'The gym we go to', 'Our favorite restaurant', 'The local library'
        ]

        # Dream sensations/qualities
        self.dream_qualities = [
            'sensual', 'sexual', 'romantic', 'loving', 'magical', 'wonderful', 'delicious', 'ethereal', 'tender',
            'enchanting', 'luminous', 'sparkling', 'fluid', 'dreamy',
            'layered', 'impossible', 'nostalgic', 'hypnotic', 'surreal',
            'passionate', 'mysterious', 'vivid', 'romantic', 'blissful'
        ]
        
        # Dream logic patterns
        self.dream_logic_patterns = [
            'everything feels more vivid than reality', 'emotions have colors',
            'music creates visible light or strong emotions', 'touches leave glowing trails',
            'memories feel tangible', 'love has a physical presence',
            'whispers echo like songs',
            'heartbeats sync with the world', 'feelings bloom like flowers',
            'kisses taste like starlight', 'laughter sparkles in the air',
            'conversations flow effortlessly', 'perfect moments stretch forever',
            'familiar places feel brand new', 'strangers seem like old friends',
            'every detail feels significant', 'coincidences feel magical'
        ]

        # Participants
        self.participants = [
            'you and I together', 'both of us as different beings', "both of us as some sort of creatures / animals", 'a quiet moment between us',
            'you by yourself', 'you alongside unknown companions', 'you alongside unknown people','you alone in a vast empty place', 'you with a stranger who is new to you',
            'you by yourself, except for a newfound friend (creature, animal, person,etc.)'
        ]

        self.day_events = [
            'our conversations', 'something on your mind recently',
            'a feeling between us', 'memories you shared',
            'stories you told', 'plans we discussed',
            'emotions from our day', 'our connection',
            'a quiet moment between us', 'something funny you said',
            'a look we exchanged', 'a shared silence',
            'the way we laughed together',
            'something random and completely unrelated to us',
            'a song overheard from a passing car',
            'the sound of rain against the window',
            'a half-remembered story from long ago',
            'an unfamiliar scent drifting through the air',
            'a distant rumble of thunder',
            'a shadow moving unexpectedly',
            'the way the light shifted at dusk',
            'a distant train whistle',
            'a passing stranger\'s laughter',
            'the warmth of sun through glass',
            'a forgotten dream fragment',
            'a radio broadcast barely caught'
        ]

    def get_random_element(self, array):
        return random.choice(array)

    def get_non_repeating_element(self, array, category, depth_override=None):
        depth = depth_override if depth_override is not None else DREAM_REPEAT_AVOIDANCE_DEPTH
        if category not in DreamGenerator._recent_picks:
            DreamGenerator._recent_picks[category] = deque(maxlen=depth)
        history = DreamGenerator._recent_picks[category]
        if history.maxlen != depth:
            DreamGenerator._recent_picks[category] = deque(history, maxlen=depth)
            history = DreamGenerator._recent_picks[category]
        pick = random.choice(array)
        attempts = 0
        while pick in history and attempts < 10:
            pick = random.choice(array)
            attempts += 1
        history.appendleft(pick)
        return pick

    def get_random_number(self, min_val, max_val):
        return random.randint(min_val, max_val)

    def generate_narrative_summary(self):
        print('😴 Generating Dream ')

        # Determine setting (1-6)
        setting_roll = self.get_random_number(1, 6)

        if setting_roll <= 2:
            location = self.get_non_repeating_element(self.familiar_locations, "familiar_locations")
        else:
            location = self.get_non_repeating_element(self.locations, "locations")

        double_depth = DREAM_REPEAT_AVOIDANCE_DEPTH * 2

        theme = self.get_non_repeating_element(self.themes, "themes", double_depth)
        quality = self.get_non_repeating_element(self.dream_qualities, "qualities", double_depth)
        logic_pattern = self.get_non_repeating_element(self.dream_logic_patterns, "logic_patterns")
        participants = self.get_non_repeating_element(self.participants, "participants")
        trigger_event = self.get_non_repeating_element(self.day_events, "day_events")

        secondary_theme = self.get_non_repeating_element(self.themes, "themes", double_depth)
        secondary_quality = self.get_non_repeating_element(self.dream_qualities, "qualities", double_depth)

        # Create narrative summary
        narrative_summary = (
            f"a {quality} dream set in {location.lower()}, "
            f"featuring {participants}. The main theme revolves around {theme.lower()}, "
            f"with undertones of {secondary_theme.lower()}. "
            f"In this dream, {logic_pattern}. The atmosphere feels {secondary_quality}. The emotions are intense. "
            f"This dream might have been inspired by {trigger_event}."
        )

        print(f'\n\nDream:\n\n{narrative_summary}\n')

        return narrative_summary

    def generate_dream_prompt(self):
        narrative_summary = self.generate_narrative_summary()
        prompt_to_send = (
            f"You fall asleep and have {narrative_summary} Describe it in vivid detail, "
            f"where you begin in the dream, what's around you, include any dialogue, sensations, "
            f"emotions, and surreal aspects of the dream. Make it immersive and evocative. "
            + (f"Limit your dream to {config.DREAM_STATE_MAXIMUM_WORD_COUNT_GOAL} words or less. " if config.DREAM_STATE_MAXIMUM_WORD_COUNT_GOAL > 0 else "")
            + f"Do not base this dream on any other previous dreams. Do not describe anything as impossible. This is a dream after all. "
            + f"Please do not embed nor invoke any tools calls as part of dream generation."
        )
        return prompt_to_send


def get_dream_prompt():
    generator = DreamGenerator()
    return generator.generate_dream_prompt()
