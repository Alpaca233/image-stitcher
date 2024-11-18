# coordinate_stitcher.py
# napari + stitching libs 
import os
import sys
from qtpy.QtCore import *
from threading import Thread, Lock

import psutil
import shutil
import random
import json
import time
import math
from lxml import etree
import numpy as np
import pandas as pd
import cv2
import dask.array as da
from dask_image.imread import imread as dask_imread
from skimage.registration import phase_cross_correlation
from skimage import exposure
import ome_zarr
import zarr
from aicsimageio.writers import OmeTiffWriter
from aicsimageio.writers import OmeZarrWriter
from aicsimageio import types
from basicpy import BaSiC
from parameters import StitchingParameters

class CoordinateStitcher(QThread):
    
    update_progress = Signal(int, int)
    getting_flatfields = Signal()
    starting_stitching = Signal()
    starting_saving = Signal(bool)
    finished_saving = Signal(str, object)

    def __init__(self, params: StitchingParameters):
        super().__init__()
        
        # Validate and store parameters
        self.params = params
        params.validate()
        
        # Core attributes from parameters
        self.input_folder = params.input_folder
        self.output_format = params.output_format
        self.output_folder = params.stitched_folder
        os.makedirs(self.output_folder, exist_ok=True)
        
        # Default merge parameters to False
        self.merge_timepoints = params.merge_timepoints if hasattr(params, 'merge_timepoints') else False
        print("merge time points", self.merge_timepoints)
        self.merge_hcs_regions = params.merge_hcs_regions if hasattr(params, 'merge_hcs_regions') else False
        print("merge hcs regions", self.merge_hcs_regions)
        # Conditional setup for outputs based on merge options
        self.per_timepoint_region_output_template = os.path.join(
            self.output_folder, "{timepoint}_stitched", "{region}_stitched" + self.output_format
        )
        
        if self.merge_timepoints:
            self.region_time_series_dir = os.path.join(self.output_folder, "region_time_series")
            os.makedirs(self.region_time_series_dir, exist_ok=True)
            self.merged_timepoints_output_template = os.path.join(
                self.region_time_series_dir, "{region}_time_series" + self.output_format
            )
        
        if self.merge_hcs_regions:
            self.hcs_time_points_dir = os.path.join(self.output_folder, "hcs_time_points")
            os.makedirs(self.hcs_time_points_dir, exist_ok=True)
            self.merged_hcs_output_template = os.path.join(
                self.hcs_time_points_dir, "{timepoint}_hcs" + self.output_format
            )
        
        if self.merge_timepoints and self.merge_hcs_regions:
            self.complete_hcs_output_path = os.path.join(
                self.hcs_time_points_dir, "complete_hcs" + self.output_format
            )
        
        self.apply_flatfield = params.apply_flatfield
        self.use_registration = params.use_registration
        
        if self.use_registration:
            self.registration_channel = params.registration_channel
            self.registration_z_level = params.registration_z_level
        
        # Initialize state
        self.coordinates_df = None
        self.pixel_size_um = None
        self.acquisition_params = None
        self.time_points = []
        self.regions = []
        self.overlap_percent = params.overlap_percent
        self.scan_pattern = params.scan_pattern        
        self.init_stitching_parameters()

    def init_stitching_parameters(self):
        self.is_rgb = {}
        self.channel_names = []
        self.mono_channel_names = []
        self.channel_colors = []
        self.num_z = self.num_c = self.num_t = 1
        self.input_height = self.input_width = 0
        self.num_pyramid_levels = 5
        self.flatfields = {}
        self.stitching_data = {}
        self.dtype = np.uint16
        self.chunks = None
        self.h_shift = (0, 0)
        if self.scan_pattern == 'S-Pattern':
            self.h_shift_rev = (0, 0)
            self.h_shift_rev_odd = 0 # 0 reverse even rows, 1 reverse odd rows
        self.v_shift = (0, 0)
        self.x_positions = set()
        self.y_positions = set()

    def get_time_points(self):
        self.time_points = [d for d in os.listdir(self.input_folder) if os.path.isdir(os.path.join(self.input_folder, d)) and d.isdigit()]
        self.time_points.sort(key=int)

        if len(self.time_points) > 0:
            image_folder = os.path.join(self.input_folder, str(self.time_points[0]))
            image_files = sorted([f for f in os.listdir(image_folder) if f.endswith(('.bmp', '.tiff')) and 'focus_camera' not in f])
            first_image_filename = image_files[0]
            self.normal_naming = not ('x' in image_files[0] and 'y' in image_files[0] and 'z' in image_files[0])

        return self.time_points

    def extract_acquisition_parameters(self):
        acquistion_params_path = os.path.join(self.input_folder, 'acquisition parameters.json')
        with open(acquistion_params_path, 'r') as file:
            self.acquisition_params = json.load(file)

    def get_pixel_size_from_params(self):
        obj_mag = self.acquisition_params['objective']['magnification']
        obj_tube_lens_mm = self.acquisition_params['objective']['tube_lens_f_mm']
        sensor_pixel_size_um = self.acquisition_params['sensor_pixel_size_um']
        tube_lens_mm = self.acquisition_params['tube_lens_mm']

        obj_focal_length_mm = obj_tube_lens_mm / obj_mag
        actual_mag = tube_lens_mm / obj_focal_length_mm
        self.pixel_size_um = sensor_pixel_size_um / actual_mag
        print("pixel_size_um:", self.pixel_size_um)

    
    def parse_filenames(self):
        """
        Parses image filenames and matches them to coordinates for stitching.
        Handles multiple timepoints and stores the stitching data across all timepoints.
        """
        self.extract_acquisition_parameters()
        self.get_pixel_size_from_params()

        self.stitching_data = {}
        self.regions = set()
        self.channel_names = set()
        max_z = 0
        max_fov = 0

        # Iterate over each timepoint
        for t, time_point in enumerate(self.time_points):
            image_folder = os.path.join(self.input_folder, str(time_point))
            coordinates_path = os.path.join(self.input_folder, time_point, 'coordinates.csv')
            
            # Handle missing coordinate files gracefully
            try:
                coordinates_df = pd.read_csv(coordinates_path)
            except FileNotFoundError:
                print(f"Warning: coordinates.csv not found for timepoint {time_point}")
                continue
            
            print(f"Processing timepoint {time_point}, image folder: {image_folder}")

            # Get the image files for this timepoint
            image_files = sorted([f for f in os.listdir(image_folder) if f.endswith(('.bmp', '.tiff')) and 'focus_camera' not in f])

            if not image_files:
                print(f"Warning: No valid files found in directory for timepoint {time_point}.")
                continue

            # Process each image file for the current timepoint
            for file in image_files:
                # Split the filename to extract region, fov, z_level, and channel
                parts = file.split('_', 3)
                region, fov, z_level, channel = parts[0], int(parts[1]), int(parts[2]), os.path.splitext(parts[3])[0]
                channel = channel.replace("_", " ").replace("full ", "full_")

                # Filter the coordinates dataframe based on region, fov, and z_level
                coord_row = coordinates_df[(coordinates_df['region'] == region) & 
                                           (coordinates_df['fov'] == fov) & 
                                           (coordinates_df['z_level'] == z_level)]

                if coord_row.empty:
                    print(f"Warning: No matching coordinates found for file {file}")
                    continue

                coord_row = coord_row.iloc[0]  # Take the first matching row

                # Create a key for storing the stitching data
                key = (t, region, fov, z_level, channel)

                # Store the stitching data for this image
                self.stitching_data[key] = {
                    'filepath': os.path.join(image_folder, file),
                    'x': coord_row['x (mm)'],
                    'y': coord_row['y (mm)'],
                    'z': coord_row['z (um)'],
                    'channel': channel,
                    'z_level': z_level,
                    'region': region,
                    'fov_idx': fov,
                    't': t
                }

                # Add region and channel names to the sets
                self.regions.add(region)
                self.channel_names.add(channel)

                # Update max_z and max_fov values
                max_z = max(max_z, z_level)
                max_fov = max(max_fov, fov)

        # After processing all timepoints, finalize the list of regions and channels
        self.regions = sorted(self.regions)
        self.channel_names = sorted(self.channel_names)
        
        # Calculate number of timepoints (t), Z levels, and FOVs per region
        self.num_t = len(self.time_points)
        self.num_z = max_z + 1
        self.num_fovs_per_region = max_fov + 1

        # Print out information about the dataset
        print(f"Regions: {self.regions}, Channels: {self.channel_names}")
        print(f"FOV dimensions: {self.input_height}x{self.input_width}")
        print(f"{self.num_z} Z levels, {self.num_t} Time points")
        print(f"{self.num_c} Channels: {self.mono_channel_names}")
        print(f"{len(self.regions)} Regions: {self.regions}")
        print(f"Number of FOVs per region: {self.num_fovs_per_region}")
        
        # Set up image parameters based on the first image
        first_key = list(self.stitching_data.keys())[0]
        first_region = self.stitching_data[first_key]['region']
        first_fov = self.stitching_data[first_key]['fov_idx']
        first_z_level = self.stitching_data[first_key]['z_level']
        first_image = dask_imread(self.stitching_data[first_key]['filepath'])[0]

        self.dtype = first_image.dtype
        if len(first_image.shape) == 2:
            self.input_height, self.input_width = first_image.shape
        elif len(first_image.shape) == 3:
            self.input_height, self.input_width = first_image.shape[:2]
        else:
            raise ValueError(f"Unexpected image shape: {first_image.shape}")
        self.chunks = (1, 1, 1, 512, 512)
            
        # Set up final monochrome channels
        self.mono_channel_names = []
        for channel in self.channel_names:
            channel_key = (t, first_region, first_fov, first_z_level, channel)
            channel_image = dask_imread(self.stitching_data[channel_key]['filepath'])[0]
            if len(channel_image.shape) == 3 and channel_image.shape[2] == 3:
                self.is_rgb[channel] = True
                channel = channel.split('_')[0]
                self.mono_channel_names.extend([f"{channel}_R", f"{channel}_G", f"{channel}_B"])
            else:
                self.is_rgb[channel] = False
                self.mono_channel_names.append(channel)

        self.num_c = len(self.mono_channel_names)
        self.channel_colors = [self.get_channel_color(name) for name in self.mono_channel_names]

    def get_channel_color(self, channel_name):
        color_map = {
            '405': 0x0000FF,  # Blue
            '488': 0x00FF00,  # Green
            '561': 0xFFCF00,  # Yellow
            '638': 0xFF0000,  # Red
            '730': 0x770000,  # Dark Red"
            '_B': 0x0000FF,  # Blue
            '_G': 0x00FF00,  # Green
            '_R': 0xFF0000  # Red
        }
        for key in color_map:
            if key in channel_name:
                return color_map[key]
        return 0xFFFFFF  # Default to white if no match found

    def calculate_output_dimensions(self, region):
        region_data = [tile_info for key, tile_info in self.stitching_data.items() if key[1] == region]
        
        if not region_data:
            raise ValueError(f"No data found for region {region}")

        self.x_positions = sorted(set(tile_info['x'] for tile_info in region_data))
        self.y_positions = sorted(set(tile_info['y'] for tile_info in region_data))

        if self.use_registration: # Add extra space for shifts 
            num_cols = len(self.x_positions)
            num_rows = len(self.y_positions)

            if self.scan_pattern == 'S-Pattern':
                max_h_shift = (max(self.h_shift[0], self.h_shift_rev[0]), max(self.h_shift[1], self.h_shift_rev[1]))
            else:
                max_h_shift = self.h_shift

            width_pixels = int(self.input_width + ((num_cols - 1) * (self.input_width + max_h_shift[1]))) # horizontal width with overlap
            width_pixels += abs((num_rows - 1) * self.v_shift[1]) # horizontal shift from vertical registration
            height_pixels = int(self.input_height + ((num_rows - 1) * (self.input_height + self.v_shift[0]))) # vertical height with overlap
            height_pixels += abs((num_cols - 1) * max_h_shift[0]) # vertical shift from horizontal registration
 
        else: # Use coordinates shifts 
            width_mm = max(self.x_positions) - min(self.x_positions) + (self.input_width * self.pixel_size_um / 1000)
            height_mm = max(self.y_positions) - min(self.y_positions) + (self.input_height * self.pixel_size_um / 1000)

            width_pixels = int(np.ceil(width_mm * 1000 / self.pixel_size_um))
            height_pixels = int(np.ceil(height_mm * 1000 / self.pixel_size_um))

        # Get the number of rows and columns
        if len(self.regions) > 1:
            rows, columns = self.get_rows_and_columns()
            max_dimension = max(len(rows), len(columns))
        else:
            max_dimension = 1

        # Calculate the number of pyramid levels
        self.num_pyramid_levels = math.ceil(np.log2(max(width_pixels, height_pixels) / 1024 * max_dimension))
        print("# Pyramid levels:", self.num_pyramid_levels)
        return width_pixels, height_pixels

    def init_output(self, region):
        # region dim
        width, height = self.calculate_output_dimensions(region)
        # create zeros with the right shape/dtype per timepoint per region
        output_shape = (1, self.num_c, self.num_z, height, width)
        print(f"Output shape for region {region}: {output_shape}")
        return da.zeros(output_shape, dtype=self.dtype, chunks=self.chunks)

    def get_flatfields(self, progress_callback=None):
        def process_images(images, channel_name):
            if images.size == 0:
                print(f"WARNING: No images found for channel {channel_name}")
                return

            if images.ndim != 3 and images.ndim != 4:
                raise ValueError(f"Images must be 3 or 4-dimensional array, with dimension of (T, Y, X) or (T, Z, Y, X). Got shape {images.shape}")

            basic = BaSiC(get_darkfield=False, smoothness_flatfield=1)
            basic.fit(images)
            channel_index = self.mono_channel_names.index(channel_name)
            self.flatfields[channel_index] = basic.flatfield
            if progress_callback:
                progress_callback(channel_index + 1, self.num_c)

        for channel in self.channel_names:
            print(f"Calculating {channel} flatfield...")
            images = []
            for t in self.time_points:
                time_images = [dask_imread(tile['filepath'])[0] for key, tile in self.stitching_data.items() if tile['channel'] == channel and key[0] == int(t)]
                if not time_images:
                    print(f"WARNING: No images found for channel {channel} at timepoint {t}")
                    continue
                random.shuffle(time_images)
                selected_tiles = time_images[:min(32, len(time_images))]
                images.extend(selected_tiles)

            if not images:
                print(f"WARNING: No images found for channel {channel} across all timepoints")
                continue

            images = np.array(images)

            if images.ndim == 3:
                # Images are in the shape (N, Y, X)
                process_images(images, channel)
            elif images.ndim == 4:
                if images.shape[-1] == 3:
                    # Images are in the shape (N, Y, X, 3) for RGB images
                    images_r = images[..., 0]
                    images_g = images[..., 1]
                    images_b = images[..., 2]
                    channel = channel.split('_')[0]
                    process_images(images_r, channel + '_R')
                    process_images(images_g, channel + '_G')
                    process_images(images_b, channel + '_B')
                else:
                    # Images are in the shape (N, Z, Y, X)
                    process_images(images, channel)
            else:
                raise ValueError(f"Unexpected number of dimensions in images array: {images.ndim}")

    def calculate_shifts(self, region):
        region_data = [v for k, v in self.stitching_data.items() if k[1] == region]
        
        # Get unique x and y positions
        x_positions = sorted(set(tile['x'] for tile in region_data))
        y_positions = sorted(set(tile['y'] for tile in region_data))
        
        # Initialize shifts
        self.h_shift = (0, 0)
        self.v_shift = (0, 0)

        # Set registration channel if not already set
        if not self.registration_channel:
            self.registration_channel = self.channel_names[0]
        elif self.registration_channel not in self.channel_names:
            print(f"Warning: Specified registration channel '{self.registration_channel}' not found. Using {self.channel_names[0]}.")
            self.registration_channel = self.channel_names[0]


        if self.overlap_percent != 0:
            max_x_overlap = round(self.input_width * self.overlap_percent / 2 / 100)
            max_y_overlap = round(self.input_height * self.overlap_percent / 2 / 100)
            print(f"Expected shifts - Horizontal: {(0, -max_x_overlap)}, Vertical: {(-max_y_overlap , 0)}")

        else: # Calculate estimated overlap from acquisition parameters
            dx_mm = self.acquisition_params['dx(mm)']
            dy_mm = self.acquisition_params['dy(mm)']
            obj_mag = self.acquisition_params['objective']['magnification']
            obj_tube_lens_mm = self.acquisition_params['objective']['tube_lens_f_mm']
            sensor_pixel_size_um = self.acquisition_params['sensor_pixel_size_um']
            tube_lens_mm = self.acquisition_params['tube_lens_mm']

            obj_focal_length_mm = obj_tube_lens_mm / obj_mag
            actual_mag = tube_lens_mm / obj_focal_length_mm
            self.pixel_size_um = sensor_pixel_size_um / actual_mag
            print("pixel_size_um:", self.pixel_size_um)

            dx_pixels = dx_mm * 1000 / self.pixel_size_um
            dy_pixels = dy_mm * 1000 / self.pixel_size_um
            print("dy_pixels", dy_pixels, ", dx_pixels:", dx_pixels)

            max_x_overlap = round(abs(self.input_width - dx_pixels) * 1.05)
            max_y_overlap = round(abs(self.input_height - dy_pixels) * 1.05)
            print("objective calculated - vertical overlap:", max_y_overlap, ", horizontal overlap:", max_x_overlap)

        # Find center positions
        center_x_index = (len(x_positions) - 1) // 2
        center_y_index = (len(y_positions) - 1) // 2
        
        center_x = x_positions[center_x_index]
        center_y = y_positions[center_y_index]

        right_x = None
        bottom_y = None

        # Calculate horizontal shift
        if center_x_index + 1 < len(x_positions):
            right_x = x_positions[center_x_index + 1]
            center_tile = self.get_tile(region, center_x, center_y, self.registration_channel, self.registration_z_level)
            right_tile = self.get_tile(region, right_x, center_y, self.registration_channel, self.registration_z_level)
            
            if center_tile is not None and right_tile is not None:
                self.h_shift = self.calculate_horizontal_shift(center_tile, right_tile, max_x_overlap)
            else:
                print(f"Warning: Missing tiles for horizontal shift calculation in region {region}.")
        
        # Calculate vertical shift
        if center_y_index + 1 < len(y_positions):
            bottom_y = y_positions[center_y_index + 1]
            center_tile = self.get_tile(region, center_x, center_y, self.registration_channel, self.registration_z_level)
            bottom_tile = self.get_tile(region, center_x, bottom_y, self.registration_channel, self.registration_z_level)
            
            if center_tile is not None and bottom_tile is not None:
                self.v_shift = self.calculate_vertical_shift(center_tile, bottom_tile, max_y_overlap)
            else:
                print(f"Warning: Missing tiles for vertical shift calculation in region {region}.")

        if self.scan_pattern == 'S-Pattern' and right_x and bottom_y:
            center_tile = self.get_tile(region, center_x, bottom_y, self.registration_channel, self.registration_z_level)
            right_tile = self.get_tile(region, right_x, bottom_y, self.registration_channel, self.registration_z_level)

            if center_tile is not None and right_tile is not None:
                self.h_shift_rev = self.calculate_horizontal_shift(center_tile, right_tile, max_x_overlap)
                self.h_shift_rev_odd = center_y_index % 2 == 0
                print(f"Bi-Directional Horizontal Shift - Reverse Horizontal: {self.h_shift_rev}")
            else:
                print(f"Warning: Missing tiles for reverse horizontal shift calculation in region {region}.")

        print(f"Calculated Uni-Directional Shifts - Horizontal: {self.h_shift}, Vertical: {self.v_shift}")

    def calculate_horizontal_shift(self, img1, img2, max_overlap):
        img1 = self.normalize_image(img1)
        img2 = self.normalize_image(img2)

        margin = int(img1.shape[0] * 0.2)  # 20% margin
        img1_overlap = img1[margin:-margin, -max_overlap:]
        img2_overlap = img2[margin:-margin, :max_overlap]

        self.visualize_image(img1_overlap, img2_overlap, 'horizontal')

        shift, error, diffphase = phase_cross_correlation(img1_overlap, img2_overlap, upsample_factor=10)
        return round(shift[0]), round(shift[1] - img1_overlap.shape[1])

    def calculate_vertical_shift(self, img1, img2, max_overlap):
        img1 = self.normalize_image(img1)
        img2 = self.normalize_image(img2)

        margin = int(img1.shape[1] * 0.2)  # 20% margin
        img1_overlap = img1[-max_overlap:, margin:-margin]
        img2_overlap = img2[:max_overlap, margin:-margin]

        self.visualize_image(img1_overlap, img2_overlap, 'vertical')

        shift, error, diffphase = phase_cross_correlation(img1_overlap, img2_overlap, upsample_factor=10)
        return round(shift[0] - img1_overlap.shape[0]), round(shift[1])

    def get_tile(self, region, x, y, channel, z_level):
        for key, value in self.stitching_data.items():
            if (key[1] == region and 
                value['x'] == x and 
                value['y'] == y and 
                value['channel'] == channel and 
                value['z_level'] == z_level):
                try:
                    return dask_imread(value['filepath'])[0]
                except FileNotFoundError:
                    print(f"Warning: Tile file not found: {value['filepath']}")
                    return None
        print(f"Warning: No matching tile found for region {region}, x={x}, y={y}, channel={channel}, z={z_level}")
        return None

    def normalize_image(self, img):
        img_min, img_max = img.min(), img.max()
        img_normalized = (img - img_min) / (img_max - img_min)
        scale_factor = np.iinfo(self.dtype).max if np.issubdtype(self.dtype, np.integer) else 1
        return (img_normalized * scale_factor).astype(self.dtype)

    def visualize_image(self, img1, img2, title):
        try:
            # Ensure images are numpy arrays
            img1 = np.asarray(img1)
            img2 = np.asarray(img2)

            if title == 'horizontal':
                combined_image = np.hstack((img1, img2))
            else:
                combined_image = np.vstack((img1, img2))
            
            # Convert to uint8 for saving as PNG
            combined_image_uint8 = (combined_image / np.iinfo(self.dtype).max * 255).astype(np.uint8)
            
            cv2.imwrite(f"{self.input_folder}/{title}.png", combined_image_uint8)
            
            print(f"Saved {title}.png successfully")
        except Exception as e:
            print(f"Error in visualize_image: {e}")

    def stitch_and_save_region(self, timepoint, region, progress_callback=None):
        """Stitch and save single region for a specific timepoint."""
        # Initialize output array 
        stitched_images = self.init_output(region)
        region_data = {k: v for k, v in self.stitching_data.items() if k[0] == timepoint and k[1] == region}
        total_tiles = len(region_data)
        processed_tiles = 0

        x_min = min(self.x_positions)
        y_min = min(self.y_positions)

        # Process each tile with progress tracking
        for key, tile_info in region_data.items():
            t, _, fov, z_level, channel = key
            tile = dask_imread(tile_info['filepath'])[0]
            
            if self.use_registration:
                self.col_index = self.x_positions.index(tile_info['x'])
                self.row_index = self.y_positions.index(tile_info['y'])

                if self.scan_pattern == 'S-Pattern' and self.row_index % 2 == self.h_shift_rev_odd:
                    h_shift = self.h_shift_rev
                else:
                    h_shift = self.h_shift

                x_pixel = int(self.col_index * (self.input_width + h_shift[1]))
                y_pixel = int(self.row_index * (self.input_height + self.v_shift[0]))

                if h_shift[0] < 0:
                    y_pixel += int((len(self.x_positions) - 1 - self.col_index) * abs(h_shift[0]))
                else:
                    y_pixel += int(self.col_index * h_shift[0])

                if self.v_shift[1] < 0:
                    x_pixel += int((len(self.y_positions) - 1 - self.row_index) * abs(self.v_shift[1]))
                else:
                    x_pixel += int(self.row_index * self.v_shift[1])
            else:
                x_pixel = int((tile_info['x'] - x_min) * 1000 / self.pixel_size_um)
                y_pixel = int((tile_info['y'] - y_min) * 1000 / self.pixel_size_um)

            # Place tile and update progress
            if len(tile.shape) == 2:
                channel_idx = self.mono_channel_names.index(channel)
                self.place_single_channel_tile(stitched_images, tile, x_pixel, y_pixel, z_level, channel_idx, 0)
                processed_tiles += 1
            elif len(tile.shape) == 3:
                if tile.shape[2] == 3:
                    channel = channel.split('_')[0]
                    for i, color in enumerate(['R', 'G', 'B']):
                        channel_idx = self.mono_channel_names.index(f"{channel}_{color}")
                        self.place_single_channel_tile(stitched_images, tile[:,:,i], x_pixel, y_pixel, z_level, channel_idx, 0)
                    processed_tiles += 1
                elif tile.shape[0] == 1:
                    channel_idx = self.mono_channel_names.index(channel)
                    self.place_single_channel_tile(stitched_images, tile[0], x_pixel, y_pixel, z_level, channel_idx, 0)
                    processed_tiles += 1

            # Update progress if callback provided
            if progress_callback:
                progress_callback(processed_tiles, total_tiles)

        # Save the region
        self.starting_saving.emit(False)
        self.save_region_zarr(timepoint, region, stitched_images)

    def save_region_zarr(self, timepoint, region, stitched_data):
        """Save stitched region data as OME-ZARR."""
        # Ensure output directory exists
        output_path = os.path.join(self.output_folder, f"{timepoint}_stitched", 
                                  f"{region}_stitched.ome.zarr")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Create zarr store and group
        store = zarr.DirectoryStore(output_path)
        root = zarr.group(store=store)

        # Generate pyramid levels
        pyramid = self.generate_pyramid(stitched_data, self.num_pyramid_levels)

        # Create coordinate transformations for each level
        coordinate_transformations = []
        for level in range(self.num_pyramid_levels):
            scale_factor = 2 ** level
            coordinate_transformations.append([{
                "type": "scale",
                "scale": [1, 1, self.acquisition_params.get("dz(um)", 1),
                         self.pixel_size_um * scale_factor,
                         self.pixel_size_um * scale_factor]
            }])

        # Define axes
        axes = [
            {"name": "t", "type": "time", "unit": "second"},
            {"name": "c", "type": "channel"},
            {"name": "z", "type": "space", "unit": "micrometer"},
            {"name": "y", "type": "space", "unit": "micrometer"},
            {"name": "x", "type": "space", "unit": "micrometer"}
        ]

        # Write multiscales metadata
        datasets = [{
            "path": str(i),
            "coordinateTransformations": coord_trans
        } for i, coord_trans in enumerate(coordinate_transformations)]

        ome_zarr.writer.write_multiscales_metadata(root, datasets, axes=axes)

        # Write the actual data
        ome_zarr.writer.write_multiscale(
            pyramid=pyramid,
            group=root,
            axes="tczyx",
            coordinate_transformations=[d["coordinateTransformations"] for d in datasets],
            storage_options=dict(chunks=self.chunks)
        )

        # Add OMERO metadata
        root.attrs["omero"] = {
            "name": f"{region}_t{timepoint}",
            "version": "0.4",
            "channels": [{
                "label": name,
                "color": f"{color:06X}",
                "window": {
                    "start": 0,
                    "end": np.iinfo(self.dtype).max,
                    "min": 0,
                    "max": np.iinfo(self.dtype).max
                }
            } for name, color in zip(self.mono_channel_names, self.channel_colors)]
        }

        return output_path

    def place_tile(self, stitched_images, tile, x_pixel, y_pixel, z_level, channel, t):
        if len(tile.shape) == 2:
            # Handle 2D grayscale image
            channel_idx = self.mono_channel_names.index(channel)
            self.place_single_channel_tile(stitched_images, tile, x_pixel, y_pixel, z_level, channel_idx, 0)  # Always use t=0

        elif len(tile.shape) == 3:
            if tile.shape[2] == 3:
                # Handle RGB image
                channel = channel.split('_')[0]
                for i, color in enumerate(['R', 'G', 'B']):
                    channel_idx = self.mono_channel_names.index(f"{channel}_{color}")
                    self.place_single_channel_tile(stitched_images, tile[:,:,i], x_pixel, y_pixel, z_level, channel_idx, 0)  # Always use t=0
            elif tile.shape[0] == 1:
                channel_idx = self.mono_channel_names.index(channel)
                self.place_single_channel_tile(stitched_images, tile[0], x_pixel, y_pixel, z_level, channel_idx, 0)  # Always use t=0
        else:
            raise ValueError(f"Unexpected tile shape: {tile.shape}")

    def place_single_channel_tile(self, stitched_images, tile, x_pixel, y_pixel, z_level, channel_idx, t):
        if len(stitched_images.shape) != 5:
            raise ValueError(f"Unexpected stitched_images shape: {stitched_images.shape}. Expected 5D array (t, c, z, y, x).")

        if self.apply_flatfield:
            tile = self.apply_flatfield_correction(tile, channel_idx)

        if self.use_registration:
            if self.scan_pattern == 'S-Pattern' and self.row_index % 2 == self.h_shift_rev_odd:
                h_shift = self.h_shift_rev
            else:
                h_shift = self.h_shift

            # Determine crop for tile edges
            top_crop = max(0, (-self.v_shift[0] // 2) - abs(h_shift[0]) // 2) if self.row_index > 0 else 0
            bottom_crop = max(0, (-self.v_shift[0] // 2) - abs(h_shift[0]) // 2) if self.row_index < len(self.y_positions) - 1 else 0
            left_crop = max(0, (-h_shift[1] // 2) - abs(self.v_shift[1]) // 2) if self.col_index > 0 else 0
            right_crop = max(0, (-h_shift[1] // 2) - abs(self.v_shift[1]) // 2) if self.col_index < len(self.x_positions) - 1 else 0

            # Apply cropping to the tile
            tile = tile[top_crop:tile.shape[0]-bottom_crop, left_crop:tile.shape[1]-right_crop]

            # Adjust x_pixel and y_pixel based on cropping
            x_pixel += left_crop
            y_pixel += top_crop

        # Calculate end points based on stitched_images shape
        y_end = min(y_pixel + tile.shape[0], stitched_images.shape[3])
        x_end = min(x_pixel + tile.shape[1], stitched_images.shape[4])

        # Extract the tile slice we'll use
        tile_slice = tile[:y_end-y_pixel, :x_end-x_pixel]

        try:
            # Place the tile slice - use t=0 since we're working with 1-timepoint arrays
            stitched_images[0, channel_idx, z_level, y_pixel:y_end, x_pixel:x_end] = tile_slice
        except Exception as e:
            print(f"ERROR: Failed to place tile. Details: {str(e)}")
            print(f"DEBUG: t:0, channel_idx:{channel_idx}, z_level:{z_level}, y:{y_pixel}-{y_end}, x:{x_pixel}-{x_end}")
            print(f"DEBUG: tile slice shape: {tile_slice.shape}")
            print(f"DEBUG: stitched_images shape: {stitched_images.shape}")
            print(f"DEBUG: output location shape: {stitched_images[0, channel_idx, z_level, y_pixel:y_end, x_pixel:x_end].shape}")
            raise

    def apply_flatfield_correction(self, tile, channel_idx):
        if channel_idx in self.flatfields:
            return (tile / self.flatfields[channel_idx]).clip(min=np.iinfo(self.dtype).min,
                                                              max=np.iinfo(self.dtype).max).astype(self.dtype)
        return tile

    def generate_pyramid(self, image, num_levels):
        pyramid = [image]
        for level in range(1, num_levels):
            scale_factor = 2 ** level
            factors = {0: 1, 1: 1, 2: 1, 3: scale_factor, 4: scale_factor}
            if isinstance(image, da.Array):
                downsampled = da.coarsen(np.mean, image, factors, trim_excess=True)
            else:
                block_size = (1, 1, 1, scale_factor, scale_factor)
                downsampled = downscale_local_mean(image, block_size)
            pyramid.append(downsampled)
        return pyramid

    def merge_timepoints_per_region(self):
        # For each region, load and merge its timepoints
        for region in self.regions:
            
            output_path = self.merged_timepoints_output_template.format(region=region)
            store = ome_zarr.io.parse_url(output_path, mode="w").store
            root = zarr.group(store=store)

            # Load and merge data
            merged_data = self.load_and_merge_timepoints(region)

            # Create region group and write metadata
            region_group = root.create_group(region)
            
            # Prepare dataset and transformation metadata
            datasets = [{
                "path": str(i),
                "coordinateTransformations": [{
                    "type": "scale",
                    "scale": [1, 1, self.acquisition_params.get("dz(um)", 1),
                             self.pixel_size_um * (2 ** i),
                             self.pixel_size_um * (2 ** i)]
                }]
            } for i in range(self.num_pyramid_levels)]

            axes = [
                {"name": "t", "type": "time", "unit": "second"},
                {"name": "c", "type": "channel"},
                {"name": "z", "type": "space", "unit": "micrometer"},
                {"name": "y", "type": "space", "unit": "micrometer"},
                {"name": "x", "type": "space", "unit": "micrometer"}
            ]

            # Write multiscales metadata
            ome_zarr.writer.write_multiscales_metadata(
                region_group,
                datasets,
                axes=axes,
                name=region
            )
            
            # Generate and write pyramid
            pyramid = self.generate_pyramid(merged_data, self.num_pyramid_levels)
            storage_options = {"chunks": self.chunks}
            
            ome_zarr.writer.write_multiscale(
                pyramid=pyramid,
                group=region_group,
                axes=axes,
                coordinate_transformations=[d["coordinateTransformations"] for d in datasets],
                storage_options=storage_options,
                name=region
            )
            
            # Add OMERO metadata
            region_group.attrs["omero"] = {
                "name": f"Region_{region}",
                "version": "0.4",
                "channels": [{
                    "label": name,
                    "color": f"{color:06X}",
                    "window": {"start": 0, "end": np.iinfo(self.dtype).max}
                } for name, color in zip(self.mono_channel_names, self.channel_colors)]
            }
        
        self.finished_saving.emit(output_path, self.dtype)

    def load_and_merge_timepoints(self, region):
        """Load and merge all timepoints for a specific region."""
        t_data = []
        t_shapes = []

        for t in self.time_points:
            zarr_path = os.path.join(self.output_folder, 
                                    f"{t}_stitched",
                                    f"{region}_stitched" + self.output_format)
            print(f"Loading t:{t} region:{region}, path:{zarr_path}")
            
            try:
                z = zarr.open(zarr_path, mode='r')
                t_array = da.from_array(z['0'], chunks=self.chunks)
                t_data.append(t_array)
                t_shapes.append(t_array.shape)
            except Exception as e:
                print(f"Error loading timepoint {t}, region {region}: {e}")
                continue

        if not t_data:
            raise ValueError(f"No data loaded from any timepoints for region {region}")

        # Handle single vs multiple timepoints
        if len(t_data) == 1:
            return t_data[0]
        
        # Pad arrays to largest size and concatenate
        max_shape = tuple(max(s) for s in zip(*t_shapes))
        padded_data = [self.pad_to_largest(t, max_shape) for t in t_data]
        merged_data = da.concatenate(padded_data, axis=0)
        print(f"Merged timepoints shape for region {region}: {merged_data.shape}")
        return merged_data

    def pad_to_largest(self, array, target_shape):
        """Pad array to match target shape."""
        if array.shape == target_shape:
            return array
        pad_widths = [(0, max(0, ts - s)) for s, ts in zip(array.shape, target_shape)]
        return da.pad(array, pad_widths, mode='constant', constant_values=0)

    def create_hcs_ome_zarr_per_timepoint(self):
        """Create separate HCS OME-ZARR files for each timepoint."""
        for t in self.time_points:
            
            output_path = self.merged_hcs_output_template.format(timepoint=t)
            
            store = ome_zarr.io.parse_url(output_path, mode="w").store
            root = zarr.group(store=store)
            
            # Write plate metadata
            rows = sorted(set(region[0] for region in self.regions))
            columns = sorted(set(region[1:] for region in self.regions))
            well_paths = [f"{well_id[0]}/{well_id[1:]}" for well_id in sorted(self.regions)]
            
            acquisitions = [{
                "id": 0,
                "maximumfieldcount": 1,
                "name": f"Timepoint {t} Acquisition"
            }]
            
            ome_zarr.writer.write_plate_metadata(
                root,
                rows=rows,
                columns=[str(col) for col in columns],
                wells=well_paths,
                acquisitions=acquisitions,
                name=f"HCS Dataset - Timepoint {t}",
                field_count=1
            )
            
            # Process each region (well) for this timepoint
            for region in self.regions:
                # Load existing timepoint-region data
                region_path = os.path.join(self.output_folder, 
                                         f"{t}_stitched",
                                         f"{region}_{self.output_name}")
                
                if not os.path.exists(region_path):
                    print(f"Warning: Missing data for timepoint {t}, region {region}")
                    continue
                    
                # Load data from existing zarr
                z = zarr.open(region_path, mode='r')
                data = da.from_array(z['0'])
                
                # Create well hierarchy
                row, col = region[0], region[1:]
                row_group = root.require_group(row)
                well_group = row_group.require_group(col)
                
                # Write well metadata
                ome_zarr.writer.write_well_metadata(
                    well_group,
                    images=[{"path": "0", "acquisition": 0}]
                )
                
                # Write image data
                image_group = well_group.require_group("0")
                
                # Prepare dataset and transformation metadata
                datasets = [{
                    "path": str(i),
                    "coordinateTransformations": [{
                        "type": "scale",
                        "scale": [1, 1, self.acquisition_params.get("dz(um)", 1),
                                 self.pixel_size_um * (2 ** i),
                                 self.pixel_size_um * (2 ** i)]
                    }]
                } for i in range(self.num_pyramid_levels)]

                axes = [
                    {"name": "t", "type": "time", "unit": "second"},
                    {"name": "c", "type": "channel"},
                    {"name": "z", "type": "space", "unit": "micrometer"},
                    {"name": "y", "type": "space", "unit": "micrometer"},
                    {"name": "x", "type": "space", "unit": "micrometer"}
                ]

                # Write multiscales metadata
                ome_zarr.writer.write_multiscales_metadata(
                    image_group,
                    datasets,
                    axes=axes,
                    name=f"Well_{region}_t{t}"
                )
                
                # Generate and write pyramid
                pyramid = self.generate_pyramid(data, self.num_pyramid_levels)
                storage_options = {"chunks": self.chunks}
                
                ome_zarr.writer.write_multiscale(
                    pyramid=pyramid,
                    group=image_group,
                    axes=axes,
                    coordinate_transformations=[d["coordinateTransformations"] for d in datasets],
                    storage_options=storage_options,
                    name=f"Well_{region}_t{t}"
                )
                
                # Add OMERO metadata
                image_group.attrs["omero"] = {
                    "name": f"Well_{region}_t{t}",
                    "version": "0.4",
                    "channels": [{
                        "label": name,
                        "color": f"{color:06X}",
                        "window": {"start": 0, "end": np.iinfo(self.dtype).max}
                    } for name, color in zip(self.mono_channel_names, self.channel_colors)]
                }
            
            if t == self.time_points[-1]:
                self.finished_saving.emit(output_path, self.dtype)

    def create_complete_hcs_ome_zarr(self):
        """Create complete HCS OME-ZARR with merged timepoints."""
        output_path = self.complete_hcs_output_path
        
        store = ome_zarr.io.parse_url(output_path, mode="w").store
        root = zarr.group(store=store)
        
        # Write plate metadata with correct parameters
        rows = sorted(set(region[0] for region in self.regions))
        columns = sorted(set(region[1:] for region in self.regions))
        well_paths = [f"{well_id[0]}/{well_id[1:]}" for well_id in sorted(self.regions)]
        
        acquisitions = [{
            "id": 0,
            "maximumfieldcount": 1,
            "name": "Stitched Acquisition"
        }]
        
        ome_zarr.writer.write_plate_metadata(
            root,
            rows=rows,
            columns=[str(col) for col in columns],
            wells=well_paths,
            acquisitions=acquisitions,
            name="Complete HCS Dataset",
            field_count=1
        )
        
        # Process each region (well)
        for region in self.regions:
            # Load and merge timepoints for this region
            merged_data = self.load_and_merge_timepoints(region)
            
            # Create well hierarchy
            row, col = region[0], region[1:]
            row_group = root.require_group(row)
            well_group = row_group.require_group(col)
            
            # Write well metadata
            ome_zarr.writer.write_well_metadata(
                well_group,
                images=[{"path": "0", "acquisition": 0}]
            )
            
            # Write image data
            image_group = well_group.require_group("0")
            
            # Write multiscales metadata first
            datasets = [{
                "path": str(i),
                "coordinateTransformations": [{
                    "type": "scale",
                    "scale": [1, 1, self.acquisition_params.get("dz(um)", 1),
                             self.pixel_size_um * (2 ** i),
                             self.pixel_size_um * (2 ** i)]
                }]
            } for i in range(self.num_pyramid_levels)]

            axes = [
                {"name": "t", "type": "time", "unit": "second"},
                {"name": "c", "type": "channel"},
                {"name": "z", "type": "space", "unit": "micrometer"},
                {"name": "y", "type": "space", "unit": "micrometer"},
                {"name": "x", "type": "space", "unit": "micrometer"}
            ]

            ome_zarr.writer.write_multiscales_metadata(
                image_group,
                datasets,
                axes=axes,
                name=f"Well_{region}"
            )
            
            # Generate and write pyramid data
            pyramid = self.generate_pyramid(merged_data, self.num_pyramid_levels)
            storage_options = {"chunks": self.chunks}
            
            ome_zarr.writer.write_multiscale(
                pyramid=pyramid,
                group=image_group,
                axes=axes,
                coordinate_transformations=[d["coordinateTransformations"] for d in datasets],
                storage_options=storage_options,
                name=f"Well_{region}"
            )
            
            # Add OMERO metadata
            image_group.attrs["omero"] = {
                "name": f"Well_{region}",
                "version": "0.4",
                "channels": [{
                    "label": name,
                    "color": f"{color:06X}",
                    "window": {"start": 0, "end": np.iinfo(self.dtype).max}
                } for name, color in zip(self.mono_channel_names, self.channel_colors)]
            }
        
        self.finished_saving.emit(output_path, self.dtype)

    def get_rows_and_columns(self):
        rows = sorted(set(region[0] for region in self.regions))
        columns = sorted(set(region[1:] for region in self.regions))
        return rows, columns

    def create_ome_tiff(self, stitched_images):
        output_path = os.path.join(self.input_folder, self.output_name)
        
        with TiffWriter(output_path, bigtiff=True, ome=True) as tif:
            tif.write(
                data=stitched_images,
                shape=stitched_images.shape,
                dtype=self.dtype,
                photometric='minisblack',
                planarconfig='separate',
                metadata={
                    'axes': 'TCZYX',
                    'Channel': {'Name': self.mono_channel_names},
                    'SignificantBits': stitched_images.dtype.itemsize * 8,
                    'Pixels': {
                        'PhysicalSizeX': self.pixel_size_um,
                        'PhysicalSizeXUnit': 'µm',
                        'PhysicalSizeY': self.pixel_size_um,
                        'PhysicalSizeYUnit': 'µm',
                        'PhysicalSizeZ': self.acquisition_params.get("dz(um)", 1.0),
                        'PhysicalSizeZUnit': 'µm',
                    },
                }
            )
        
        print(f"Data saved in OME-TIFF format at: {output_path}")
        self.finished_saving.emit(output_path, self.dtype)

    def run(self):
        """Main execution method handling timepoints and regions."""
        stime = time.time()
        try:
            # Initial setup
            self.get_time_points()
            self.parse_filenames()

            if self.apply_flatfield:
                print("Calculating flatfields...")
                self.getting_flatfields.emit()
                self.get_flatfields(progress_callback=self.update_progress.emit)
                print("Time to calculate flatfields:", time.time() - stime)

            # Calculate registration shifts once if using registration
            if self.use_registration:
                print(f"\nCalculating shifts for region {self.regions[0]}...")
                self.calculate_shifts(self.regions[0])

            # Process each timepoint and region
            for t in self.time_points:
                ttime = time.time()
                print(f"\nProcessing timepoint {t}")
                
                # Create timepoint output directory
                t_output_dir = os.path.join(self.output_folder, f"{t}_stitched")
                os.makedirs(t_output_dir, exist_ok=True)
                
                for region in self.regions:
                    print(f"Processing region {region}...")
                    
                    # Stitch region
                    self.starting_stitching.emit()
                    stitched_data = self.stitch_and_save_region(t, region, progress_callback=self.update_progress.emit)
                    
                print(f"Time to process timepoint {t}: {time.time() - ttime}")
            
            # Post-processing based on merge settings
            post_time = time.time()
            self.starting_saving.emit(True)
            
            if self.merge_timepoints and self.merge_hcs_regions:
                print("Creating complete HCS OME-ZARR with merged timepoints...")
                self.create_complete_hcs_ome_zarr()
            elif self.merge_timepoints:
                print("Creating merged timepoints OME-ZARR...")
                self.merge_timepoints_per_region()
            elif self.merge_hcs_regions:
                print("Creating HCS OME-ZARR per timepoint...")
                self.create_hcs_ome_zarr_per_timepoint()
            else:
                # Emit finished signal with the last saved path
                final_path = os.path.join(self.output_folder, f"{self.time_points[-1]}_stitched", 
                                         f"{self.regions[-1]}_stitched{self.output_format}")
                self.finished_saving.emit(final_path, self.dtype)

            
            print(f"Post-processing time: {time.time() - post_time}")
            print(f"Total processing time: {time.time() - stime}")
            
        except Exception as e:
            print(f"Error during processing: {e}")
            raise

    def print_zarr_structure(self, path, indent=""):
        root = zarr.open(path, mode='r')
        print(f"Zarr Tree and Metadata for: {path}")
        print(root.tree())
        print(dict(root.attrs))